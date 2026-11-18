"""The mock adapter must not combine evidence or hide source identity."""
import json
from tests.research_prompt_helpers import decode_research_prompt


def test_grouped_prompt_mock_view_retains_separate_passages_and_source_groups():
    wire = {'source_input_version': '2', 'sources': [
        {'source_id': 'one', 'title': 'First', 'source_coverage': 'partial', 'passages': [
            {'text': 'Header 2024', 'context_unit_ids': ['g1']}, {'text': 'Revenue 12', 'context_unit_ids': ['g1']}]},
        {'source_id': 'two', 'title': 'Second', 'source_coverage': 'complete', 'passages': [{'text': 'Profit 9'}]}]}
    raw = json.dumps(wire)
    view = decode_research_prompt(raw)
    assert [(x['source_id'], x['text']) for x in view['sources']] == [('one', 'Header 2024'), ('one', 'Revenue 12'), ('two', 'Profit 9')]
    assert view['sources'][0]['context_unit_ids'] == view['sources'][1]['context_unit_ids'] == ['g1']
    assert json.loads(raw) == wire
