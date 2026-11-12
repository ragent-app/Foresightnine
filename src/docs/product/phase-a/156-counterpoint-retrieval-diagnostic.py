"""Reproduce the input coverage issue in the already-used stress-test sample.

No model or network access. This is a development diagnostic, not a quality
benchmark: the source and expected passage were visible when it was written.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "151-full-pipeline-recheck"))
from common import get_task, run_analysis  # noqa: E402

task, sources = get_task("STRESS_2023")
result = run_analysis(task, sources, lambda _prompt: ({}, 0))
source = sources[0]
marker = "However, banks with concentrations in mortgages, credit cards, and"
start = source.text.index(marker)
end = source.text.index("this year.", start) + len("this year.")
covered = any(segment["character_start"] <= start and segment["character_end"] >= end
              for segment in result.segments)
print(json.dumps({"selected_ranges": [[s["character_start"], s["character_end"]] for s in result.segments],
                  "risk_sentence_range": [start, end], "risk_sentence_fully_selected": covered}, indent=2))
