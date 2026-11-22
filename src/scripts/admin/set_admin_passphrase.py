#!/usr/bin/env python3
"""Print the FORESIGHTNINE_ADMIN_PASSPHRASE_HASH env line for a passphrase read without echo.

Run from src/apps/api:  uv run python ../../scripts/admin/set_admin_passphrase.py
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "api"))
from app.admin_auth import hash_passphrase  # noqa: E402


def main() -> int:
    first = getpass.getpass("Admin passphrase (min 12 characters): ")
    if len(first) < 12:
        print("Passphrase must be at least 12 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat passphrase: ") != first:
        print("Passphrases do not match.", file=sys.stderr)
        return 1
    print(f"FORESIGHTNINE_ADMIN_PASSPHRASE_HASH='{hash_passphrase(first)}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
