"""Apply the saved real-model record_facts requests to a fresh, authorized fixture checkpoint."""
from pathlib import Path
from types import SimpleNamespace
import json

from app.research_tool_executor import ToolExecutionContext, _exec_record_facts
from app.research_tools_registry import FactsRequest
from paired import make_input


def main():
    directory = Path(__file__).resolve().parent
    rows = []
    for path in sorted((directory/'paired-results').glob('facts-*.json')):
        run = json.loads(path.read_text())
        src, q, state = make_input('facts', run['arm'])
        context = ToolExecutionContext(question=q, state=state, sources=[src], source_by_id={src.source_id:src},
                                       context=None, as_of=None, seen=set())
        for call in run['tool_io']:
            if call['tool'] == 'record_facts' and not call['id'].startswith('fixture-'):
                _exec_record_facts(context, SimpleNamespace(req=FactsRequest.model_validate(call['input'])))
        facts = state['facts']
        assert {(f['period'], f['value']) for f in facts} == {('FY2025','120'), ('FY2024','100')}
        assert len(facts) == 2
        rows.append({'run':path.stem, 'frozen_fact_rows':len(run['facts']), 'replayed_fact_rows':len(facts)})
    print(json.dumps(rows, indent=2))


if __name__=='__main__':
    main()
