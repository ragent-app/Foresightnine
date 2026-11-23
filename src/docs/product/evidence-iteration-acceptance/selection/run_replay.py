"""Run a frozen offline selection script against one archived dataset."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


SELECTION = Path(__file__).resolve().parent
PROGRAMS = (
    "retrieve.py", "candidate_retrieve.py", "final_retrieve.py",
    "fixed_pool.py", "final_fixed_pool.py", "grade.py", "input_replay.py",
)
DATA_FILES = ("inputs.json", "answers.json", "freeze.json", "protocol.md", "baseline-trace.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Directory containing the dataset's freeze.json")
    parser.add_argument("program", choices=PROGRAMS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="Arguments to the original script")
    args = parser.parse_args()

    # The original scripts locate their inputs relative to their own file.
    with TemporaryDirectory(prefix="selection-replay-") as directory:
        stage = Path(directory)
        for name in PROGRAMS:
            shutil.copyfile(SELECTION / name, stage / name)
        for name in DATA_FILES:
            shutil.copyfile(args.data_dir / name, stage / name)
        manifest = json.loads((stage / "freeze.json").read_text())
        for name, expected in manifest.items():
            if name not in PROGRAMS and name not in DATA_FILES:
                parser.error(f"Unknown frozen file in {args.data_dir}: {name}")
            if hashlib.sha256((stage / name).read_bytes()).hexdigest() != expected:
                parser.error(f"Frozen file mismatch in {args.data_dir}: {name}")
        return subprocess.run([sys.executable, stage / args.program, *args.arguments]).returncode


if __name__ == "__main__":
    sys.exit(main())
