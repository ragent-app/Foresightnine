import { pandasPercentRank } from "./index.js";

export type FactorAst =
  | { readonly op: "field"; readonly name: string }
  | { readonly op: "constant"; readonly value: number }
  | { readonly op: "add" | "sub" | "mul" | "div"; readonly left: FactorAst; readonly right: FactorAst }
  | { readonly op: "neg" | "abs" | "rank"; readonly input: FactorAst }
  | { readonly op: "rolling_mean" | "rolling_std"; readonly input: FactorAst; readonly window: number };

export interface FactorObservation {
  readonly asOf: string;
  readonly fields: Readonly<Record<string, readonly (number | null)[]>>;
}

export class FactorAstError extends Error {
  readonly code: "invalid_ast" | "unknown_ast_operator" | "unknown_field" | "invalid_data";

  constructor(code: FactorAstError["code"], message: string) {
    super(message);
    this.name = "FactorAstError";
    this.code = code;
  }
}

type ObjectRecord = { readonly [key: string]: unknown };

function isRecord(value: unknown): value is ObjectRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(value: ObjectRecord, keys: readonly string[]): boolean {
  return Object.keys(value).every((key) => keys.includes(key));
}

function child(value: ObjectRecord, key: string): unknown {
  if (!(key in value)) throw new FactorAstError("invalid_ast", `AST is missing ${key}`);
  return value[key];
}

/** Validate untrusted JSON into the finite, explicit factor DSL. */
export function validateFactorAst(value: unknown): FactorAst {
  if (!isRecord(value) || typeof value.op !== "string") throw new FactorAstError("invalid_ast", "AST node must be an object with an operator");
  switch (value.op) {
    case "field":
      if (!hasOnlyKeys(value, ["op", "name"]) || typeof value.name !== "string" || value.name.trim() === "") throw new FactorAstError("invalid_ast", "field requires only a non-empty name");
      return { op: "field", name: value.name };
    case "constant":
      if (!hasOnlyKeys(value, ["op", "value"]) || typeof value.value !== "number" || !Number.isFinite(value.value)) throw new FactorAstError("invalid_ast", "constant requires a finite number");
      return { op: "constant", value: value.value };
    case "add": case "sub": case "mul": case "div":
      if (!hasOnlyKeys(value, ["op", "left", "right"])) throw new FactorAstError("invalid_ast", `${value.op} has unknown properties`);
      return { op: value.op, left: validateFactorAst(child(value, "left")), right: validateFactorAst(child(value, "right")) };
    case "neg": case "abs": case "rank":
      if (!hasOnlyKeys(value, ["op", "input"])) throw new FactorAstError("invalid_ast", `${value.op} has unknown properties`);
      return { op: value.op, input: validateFactorAst(child(value, "input")) };
    case "rolling_mean": case "rolling_std":
      if (!hasOnlyKeys(value, ["op", "input", "window"]) || typeof value.window !== "number" || !Number.isInteger(value.window) || value.window < 1) throw new FactorAstError("invalid_ast", `${value.op} requires a positive integer window`);
      return { op: value.op, input: validateFactorAst(child(value, "input")), window: value.window };
    default:
      throw new FactorAstError("unknown_ast_operator", `unknown factor AST operator: ${value.op}`);
  }
}

function validateObservations(observations: readonly FactorObservation[]): number {
  if (observations.length === 0) throw new FactorAstError("invalid_data", "at least one factor observation is required");
  const width = Object.values(observations[0]!.fields)[0]?.length;
  if (width === undefined) throw new FactorAstError("invalid_data", "at least one field is required");
  for (const observation of observations) {
    for (const values of Object.values(observation.fields)) if (values.length !== width) throw new FactorAstError("invalid_data", "all fields must share the asset width");
  }
  return width;
}

type Matrix = Float64Array[];

function nanMatrix(rows: number, width: number): Matrix {
  return Array.from({ length: rows }, () => new Float64Array(width).fill(Number.NaN));
}

function mapUnary(input: Matrix, operation: (value: number) => number): Matrix {
  return input.map((row) => Float64Array.from(row, (value) => Number.isFinite(value) ? operation(value) : Number.NaN));
}

function mapBinary(left: Matrix, right: Matrix, operation: (left: number, right: number) => number): Matrix {
  return left.map((row, time) => Float64Array.from(row, (value, asset) => {
    const other = right[time]![asset]!;
    return Number.isFinite(value) && Number.isFinite(other) ? operation(value, other) : Number.NaN;
  }));
}

function evaluate(node: FactorAst, observations: readonly FactorObservation[], width: number): Matrix {
  switch (node.op) {
    case "field": {
      return observations.map((observation) => {
        const values = observation.fields[node.name];
        if (values === undefined) throw new FactorAstError("unknown_field", `unknown factor field: ${node.name}`);
        if (values.length !== width) throw new FactorAstError("invalid_data", `field ${node.name} has an invalid asset width`);
        return Float64Array.from(values, (value) => value === null || !Number.isFinite(value) ? Number.NaN : value);
      });
    }
    case "constant":
      return observations.map(() => new Float64Array(width).fill(node.value));
    case "add": return mapBinary(evaluate(node.left, observations, width), evaluate(node.right, observations, width), (left, right) => left + right);
    case "sub": return mapBinary(evaluate(node.left, observations, width), evaluate(node.right, observations, width), (left, right) => left - right);
    case "mul": return mapBinary(evaluate(node.left, observations, width), evaluate(node.right, observations, width), (left, right) => left * right);
    case "div": return mapBinary(evaluate(node.left, observations, width), evaluate(node.right, observations, width), (left, right) => right === 0 ? Number.NaN : left / right);
    case "neg": return mapUnary(evaluate(node.input, observations, width), (value) => -value);
    case "abs": return mapUnary(evaluate(node.input, observations, width), Math.abs);
    case "rank": return evaluate(node.input, observations, width).map((row) => pandasPercentRank(row));
    case "rolling_mean":
    case "rolling_std": {
      const input = evaluate(node.input, observations, width);
      const output = nanMatrix(input.length, width);
      for (let time = 0; time < input.length; time += 1) for (let asset = 0; asset < width; asset += 1) {
        const values: number[] = [];
        for (let cursor = Math.max(0, time - node.window + 1); cursor <= time; cursor += 1) {
          const value = input[cursor]![asset]!;
          if (Number.isFinite(value)) values.push(value);
        }
        if (values.length > 0) {
          const average = values.reduce((total, value) => total + value, 0) / values.length;
          output[time]![asset] = node.op === "rolling_mean" ? average : Math.sqrt(values.reduce((total, value) => total + (value - average) ** 2, 0) / values.length);
        }
      }
      return output;
    }
  }
}

export function evaluateFactorAst(ast: FactorAst, observations: readonly FactorObservation[]): readonly Float64Array[] {
  const validAst = validateFactorAst(ast);
  const width = validateObservations(observations);
  return evaluate(validAst, observations, width);
}
