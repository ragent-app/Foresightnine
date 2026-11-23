# Frozen replay artifacts

These are fictional cases and sources. They were kept private from implementation until the independent review and initial candidate assessment were complete; they are now disclosed and must not be treated as a fresh holdout for future tuning. The original cases, deterministic runner, task construction script and native runtime runner preserve the hashes registered in the parent baseline protocol. The blind packets were shuffled and assigned random IDs before a separate Codex development-model reviewer judged them without the mapping or answer key. `blind-mapping.json` links IDs only after adjudication. The verdict file is model output, not human review or a live product-provider response.

`replay.py` measures `research_delivery.assess` without a verifier; `hydrate_blind.py` sends the recorded blind judgments through the candidate's actual evidence-ID parsing and assessment interface. `native.py` exercises `run_research_agent` with a scripted writer and no functioning reviewer. `api_probe.py` uses authenticated local HTTP, scripted native writer calls and replays the separately obtained blind judgments as the review response; it inspects saved versions and exports. Scripted responses verify integration, not autonomous research behavior. All model, search and data-provider calls are local fakes. `baseline.json` and `native-baseline.json` are the immutable baseline outputs.

For exact replay, restore the private evaluator paths expected by the frozen scripts from the repository root. The API virtual environment is used for all commands. The API probe currently targets this workspace's absolute path, as recorded for the original run; edit that path only to relocate the workspace and identify the edit in any later evidence.

```sh
mkdir -p /tmp/foresight-quality-private
cp src/docs/product/financial-research-quality-evidence/frozen/{cases.json,blind-mapping.json} /tmp/foresight-quality-private/
cp src/docs/product/financial-research-quality-evidence/frozen/foresight-quality-blind-{packets,verdicts}.json /tmp/
src/apps/api/.venv/bin/python src/docs/product/financial-research-quality-evidence/frozen/replay.py "$PWD/src/apps/api"
src/apps/api/.venv/bin/python src/docs/product/financial-research-quality-evidence/frozen/hydrate_blind.py "$PWD/src/apps/api"
src/apps/api/.venv/bin/python src/docs/product/financial-research-quality-evidence/frozen/native.py "$PWD/src/apps/api"
src/apps/api/.venv/bin/python src/docs/product/financial-research-quality-evidence/frozen/api_probe.py
```

The frozen replay records candidate-level statuses and reasons; score a false accept when `expected` is false and `answered` true, and a false reject when `expected` true and `answered` false. Treat exceptions and unavailable assessments as not answered. For the API probe also examine the report, stored version and export for unsupported candidate text, not just the status. The test corpus and verdicts are not a statistical estimate of real-world performance.
