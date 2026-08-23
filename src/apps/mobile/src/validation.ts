export interface ValidationErrors {
  viewpoint?: string;
  capital?: string;
}

export function parseCapital(raw: string): number | null {
  const normalized = raw.replace(/[,_\s￥¥$€]/g, "").trim();
  if (!normalized || !/^\d+(?:\.\d{1,2})?$/.test(normalized)) return null;
  const value = Number(normalized);
  return Number.isFinite(value) && value > 0 ? value : null;
}

export function validateThesis(viewpoint: string, capital: string): ValidationErrors {
  const errors: ValidationErrors = {};
  const trimmed = viewpoint.trim();
  if (trimmed.length < 12) errors.viewpoint = "观点至少需要 12 个字符，包含可检验的前提。";
  if (trimmed.length > 2000) errors.viewpoint = "观点最多 2,000 个字符。";
  if (parseCapital(capital) === null) errors.capital = "请输入大于 0 的金额，例如 100000。";
  return errors;
}

export function hasValidationErrors(errors: ValidationErrors): boolean {
  return Object.values(errors).some(Boolean);
}
