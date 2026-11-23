"""Business boundaries for requirement retrieval and a source revoked mid-run."""
from app.research_library import ResearchLibrary
from app.research_library_tool import SearchLibraryRequest, run_search_library, sync_library
from tests.test_research_library import make_source


class CountingScorer:
    def __init__(self):
        self.queries = []
    def index_source(self, source, passages):
        pass
    def remove_source(self, source_id):
        pass
    def query(self, text, *, source_ids, limit):
        self.queries.append(text)
        return []


def test_requirement_expansion_is_local_and_preserves_candidate_trace():
    scorer = CountingScorer()
    source = make_source('s', 'Revenue 2025 was 120 and 2024 was 100. A change in scope contributed to growth.')
    source = source.model_copy(update={'permissions':source.permissions.model_copy(update={'display':True})})
    lib = ResearchLibrary([], vector_scorer=scorer)
    sync_library(lib, {'s': source}, {}, None, vector_scorer=scorer)
    req = SearchLibraryRequest(query='revenue', max_hits=2, requirement_ids=['r1'])
    result, _ = run_search_library(req, library=lib, source_by_id={'s': source}, versions={}, as_of=None,
                                   requirements=[{'id':'r1','question':'change in scope','status':'missing','core':True}])
    assert scorer.queries == ['revenue']
    assert result['hits'][0]['requirement_ids'] == ['r1']
    assert result['selection_trace'][0]['decision'] == 'selected'
    assert result['requirement_queries'][0]['requirement_id'] == 'r1'
    bad, _ = run_search_library(req.model_copy(update={'requirement_ids':['unknown']}), library=lib,
                                source_by_id={'s':source}, versions={}, as_of=None, requirements=[])
    assert bad == {'status':'invalid','reason':'unknown_requirement_id'}
    assert scorer.queries == ['revenue']


def test_revocation_and_removal_cannot_return_previously_indexed_text():
    source = make_source('s', 'revenue is 120')
    source = source.model_copy(update={'permissions':source.permissions.model_copy(update={'display':True})})
    lib = ResearchLibrary([], vector_scorer=None)
    sync_library(lib, {'s':source}, {}, None, vector_scorer=None)
    req = SearchLibraryRequest(query='revenue')
    assert run_search_library(req, library=lib, source_by_id={'s':source}, versions={}, as_of=None)[0]['hits']
    revoked = source.model_copy(update={'permissions':source.permissions.model_copy(update={'model_input':False})})
    sync_library(lib, {'s':revoked}, {}, None, vector_scorer=None)
    result, _ = run_search_library(req, library=lib, source_by_id={'s':revoked}, versions={}, as_of=None)
    assert not result['hits']
    assert result['library_coverage']['sources_excluded'][0]['reason'] == 'model_permission_missing'
    sync_library(lib, {'s':source}, {}, None, vector_scorer=None)
    assert run_search_library(req, library=lib, source_by_id={'s':source}, versions={}, as_of=None)[0]['hits']
    sync_library(lib, {}, {}, None, vector_scorer=None)
    assert not run_search_library(req, library=lib, source_by_id={}, versions={}, as_of=None)[0]['hits']


def test_assessed_kinds_build_distinct_queries_without_answer_content():
    from app.research_library_tool import requirement_search_query
    base = {'question':'How sustainable is growth?'}
    financial = requirement_search_query({**base, 'kind':'financial:revenue:0', 'metric':'revenue'})
    counter = requirement_search_query({**base, 'kind':'counterevidence'})
    assert financial != counter
    assert 'revenue' in financial and 'risk' in counter
    assert base['question'] in financial and base['question'] in counter
