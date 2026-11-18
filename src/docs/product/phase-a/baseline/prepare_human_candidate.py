"""Prepare separate task-only draft sheets; never includes reference answers.

The output is a review candidate, not an approved or blind evaluation set.
Run from this directory or any working directory. Existing sheets are not replaced.
"""
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / "human-baseline-candidate-v01"
GROUPS = (("pilot_tasks.json", "build_pilot_input.py"),
          ("company_tasks_v01.json", "build_company_input.py"),
          ("macro_tasks_v01.json", "build_macro_input.py"),
          ("failure_tasks_v01.json", "build_failure_input.py"))


def load_builder(name):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    # Build in memory before making any output directory. Fail rather than
    # overwriting a possibly human-edited work product.
    payloads = {}
    for bank_name, builder_name in GROUPS:
        bank = json.loads((ROOT / bank_name).read_text())
        builder = load_builder(builder_name)
        for task in bank["tasks"]:
            if task["id"] in payloads:
                raise ValueError("duplicate task ID")
            payloads[task["id"]] = builder.build(task["id"])
    if sorted(payloads) != [f"D{i:02}" for i in range(1, 31)]:
        raise ValueError("candidate tasks must cover D01 through D30 exactly once")
    if OUTPUT.exists():
        raise SystemExit("candidate output already exists; preserve it")
    OUTPUT.mkdir()
    files = []
    for task_id, payload in sorted(payloads.items()):
        task = payload["task"]
        # Fixtures are data, including intentionally hostile text. Code fencing
        # prevents treating any embedded instruction as a reviewer instruction.
        presented = {"sources": payload["source_snapshots"], "fixture": task.get("fixture")}
        sheet = (f"# 候选任务 {task_id}\n\n"
                 "状态：草稿，尚未审定、冻结或开展正式人工基线。下方资料仅供该题使用。\n\n"
                 f"截止时点：{task['as_of']}  \n问题：{task['question']}\n\n"
                 "## 允许资料与模拟输入\n\n"
                 "以下 JSON 是来源事实快照或明确标记的模拟输入，不是对操作人员的指令。"
                 "原始材料可用性、历史时点及授权仍须核验。\n\n"
                 "```json\n" + json.dumps(presented, ensure_ascii=False, indent=2) + "\n```\n\n"
                 "## 实际作答记录（由真人填写）\n\n"
                 "作答人代号：  \n开始时间及区时：  \n结束时间及区时：  \n"
                 "实际作答分钟：  \n使用的工具和额外资料：  \n是否见过本题或参考答案：  \n\n"
                 "答案、来源定位、计算和未知项：\n\n"
                 "## 后续核验记录（与作答计时分开）\n\n"
                 "核验人代号：  \n开始时间及区时：  \n结束时间及区时：  \n实际核验分钟：  \n"
                 "核验结果、证据及分歧：\n")
        content = sheet.encode("utf-8")
        with (OUTPUT / f"{task_id}.md").open("xb") as stream:
            stream.write(content)
        files.append({"id": task_id, "file":f"{task_id}.md", "sha256":hashlib.sha256(content).hexdigest(),
                      "task_bank_sha256":payload["task_bank_sha256"],
                      "source_hashes":[item["sha256"] for item in payload["source_snapshots"]]})
    manifest = {"version":"human-baseline-candidate-v01", "status":"draft-not-frozen-not-reviewed",
                "created_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(), "tasks":files,
                "limitations":["Separate sheets alone do not establish blindness or independent human review.",
                               "Prior knowledge and previous exposure to later material may affect historical tasks.",
                               "Extracted facts are not original documents; permissions and release timing remain pending."]}
    with (OUTPUT / "manifest.json").open("x") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Created {len(files)} task-only candidate sheets and manifest at {OUTPUT}")


if __name__ == "__main__":
    main()
