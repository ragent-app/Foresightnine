"""Exercise one old backtest price selector on a synthetic time series.

The method is extracted unchanged from the specified checkout via AST, so this
read-only probe needs none of the old project's third-party dependencies. It
does not execute trading, use real financial data, or modify the old checkout.
"""
import ast
import datetime
import hashlib
import json
from pathlib import Path

SOURCE = Path("/home/lxx/Openclaw-stock/openclaw/skills/backtesting/enhanced_backtest.py")
OUTPUT = Path(__file__).with_name("old-backtest-time-probe-2026-11-11.json")


def main():
    raw = SOURCE.read_bytes()
    tree = ast.parse(raw)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EnhancedBacktest"]
    if len(classes) != 1:
        raise ValueError("expected one EnhancedBacktest")
    methods = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == "_get_price_at_timestamp"]
    if len(methods) != 1:
        raise ValueError("expected one price selector")
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), methods[0]], type_ignores=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(code), str(SOURCE), "exec"), namespace)
    data = {"SYNTHETIC": [{"timestamp":"2024-01-01T10:00:00", "close":100.0},
                          {"timestamp":"2024-01-01T10:01:00", "close":200.0}]}
    query = "2024-01-01T10:00:30"
    price = namespace["_get_price_at_timestamp"](None, "SYNTHETIC", data, query)
    record = {"checked_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "source":str(SOURCE), "source_sha256":hashlib.sha256(raw).hexdigest(),
              "method":"AST extraction of unchanged method; synthetic records; one local call",
              "query_timestamp":query,"synthetic_candles":data["SYNTHETIC"],"returned_close":price,
              "later_candle_selected":price == 200.0,
              "limitations":["This is an isolated helper check, not an end-to-end strategy backtest.",
                             "Candle timestamp semantics and bar availability depend on source data.",
                             "No investment performance or real market conclusion follows."]}
    with OUTPUT.open("x") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
