# Requirement links: focused local verification

Command, run in `src/apps/api` after the planning and analysis changes:

```text
.venv/bin/python -m pytest -q tests/test_research_requirements.py
....                                                                     [100%]
```

All four collected tests passed. The deterministic tests cover old planning responses without dimensions, bounded suggested dimensions, question identity, selected-source and selected-range restrictions, invalid and duplicate identifiers, successful calculation input linkage, and analysis assembly with a simulated provider. They do not test model quality, establish semantic support, or independently verify source contents. Delivery, recovery and edit paths are verified separately by the integration owner.
