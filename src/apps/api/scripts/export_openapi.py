from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

API_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = API_ROOT.parents[1]
sys.path.insert(0, str(API_ROOT))

from app.main import app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    targets = (
        WORKSPACE_ROOT / "packages" / "contracts" / "openapi" / "v1" / "openapi.json",
        WORKSPACE_ROOT / "packages" / "contracts" / "openapi" / "v1" / "openapi.yaml",
    )
    drifted: list[Path] = []
    for target in targets:
        if args.check:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != output:
                drifted.append(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(output, encoding="utf-8")
    if drifted:
        for target in drifted:
            print(f"FAIL generated OpenAPI drift: {target}", file=sys.stderr)
        return 1
    print(("PASS" if args.check else "Generated") + f" OpenAPI {len(app.openapi()['paths'])} paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
