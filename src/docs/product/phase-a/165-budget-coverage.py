"""Offline comparison on old development samples; no provider calls."""
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
from app.research_analysis import analyze, SYSTEM_PROMPT  # noqa: E402

pairs = [("134-context-budget-paired", "GDP_THIRD"), ("134-context-budget-paired", "FED_PURCHASES"),
         ("140-trade-bank-fresh", "TRADE_FEB"), ("140-trade-bank-fresh", "STRESS_2023")]
for folder, name in pairs:
    path = HERE / folder / "common.py"
    spec = importlib.util.spec_from_file_location("old_" + folder.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    task, sources = module.get_task(name)
    for budget in (3000, 6000):
        prompts = []
        result = analyze(task["question"], sources, None, lambda p: (prompts.append(p) or {}, 0),
                         max_prompt_chars=budget - len(SYSTEM_PROMPT.encode()) - 512)
        print(json.dumps({"task": name, "budget": budget, "prompt_bytes": len(prompts[0].encode()),
                          "selected_characters": sum(s["character_end"] - s["character_start"] for s in result.segments),
                          "ranges": [[s["character_start"], s["character_end"]] for s in result.segments]}))
