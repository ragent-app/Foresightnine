import json
from app.glm_mcp_search import SearchCandidate
from app.model_protocol import ModelResponse
from app.research_runtime import run_research_agent
from tests.test_research_runtime import Model, context, turn


def test_search_merges_leads_and_preserves_query_identity():
    model = Model([turn('discover_sources', {'query': 'one', 'rationale': 'find'}, 'a'),
                   turn('discover_sources', {'query': 'two', 'rationale': 'find more'}, 'b'),
                   ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    snapshots = []
    def discover(query):
        if query == 'one':
            return [SearchCandidate('First', 'https://EXAMPLE.com/report?q=1#part', 'text', None, None, False)]
        return [{'title': 'Second', 'url': 'https://example.com/report?q=2'}]
    run_research_agent(question='Q', sources=[], model=model, context=context(), discover=discover,
                       checkpoint=lambda s: snapshots.append(json.loads(json.dumps(s))))
    assert [lead['url'] for lead in snapshots[-1]['leads']] == [
        'https://example.com/report?q=1', 'https://example.com/report?q=2']


def test_all_invalid_response_disables_connector_without_another_search():
    model = Model([turn('discover_sources', {'query': 'one', 'rationale': 'find'}, 'a'),
                   turn('discover_sources', {'query': 'two', 'rationale': 'try again'}, 'b'),
                   ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    calls = []
    def discover(query):
        calls.append(query)
        return [{'bad': 'row'}]
    result = run_research_agent(question='Q', sources=[], model=model, context=context(), discover=discover)
    assert calls == ['one']
    events = [e for e in result.events if e.get('name') == 'discover_sources']
    assert [e['reason'] for e in events] == ['search_response_invalid', 'search_response_invalid']
