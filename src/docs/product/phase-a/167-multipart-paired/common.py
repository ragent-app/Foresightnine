from pathlib import Path
from unittest.mock import patch
import importlib.util
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE_DIR = HERE.parent / "161-fresh-source-evaluation"
sys.path.insert(0, str(SOURCE_DIR))
spec = importlib.util.spec_from_file_location("source161_common", SOURCE_DIR / "common.py")
source_common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source_common)
digest, get_task, SYSTEM_PROMPT = source_common.digest, source_common.get_task, source_common.SYSTEM_PROMPT
import app.research_analysis as analysis  # noqa: E402


def selected_context(condition):
    original = analysis._select_windows
    def two_windows(text, question, length, count, *args, **kwargs):
        if count == 3:
            # The caller divides its character allowance by three; convert
            # back to a two-window width, within one character of size // 2.
            return original(text, question, length * 3 // 2, 2, *args, **kwargs)
        return original(text, question, length, count, *args, **kwargs)
    return patch.object(analysis, "_select_windows", two_windows if condition == "baseline" else original)


def run_analysis(task, sources, generate):
    return analysis.analyze(task["question"], sources, None, generate,
                            max_prompt_chars=6000 - len(SYSTEM_PROMPT.encode()) - 512)
