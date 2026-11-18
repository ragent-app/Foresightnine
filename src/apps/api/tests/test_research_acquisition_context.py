from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

from app.research_acquisition_context import build_acquisition_context
from app.research_evidence_verifier import compare_requirement_candidates
from app.research_sources import ResearchSource, SourcePermissions


def source(sid, text, permissions, method='user_supplied'):
    return ResearchSource(source_id=sid,owner_id='o',title='Report',text=text,kind='official',
        obtained_at=datetime.now(timezone.utc),content_sha256=sha256(text.encode()).hexdigest(),
        retrieval_method=method,permissions=permissions,
        **({'url':'https://data.sec.gov/api/xbrl/companyconcept/CIK0000000001/us-gaap/Revenues.json'}
           if method == 'sec_companyconcept' else {}))


def test_inflight_indexes_and_structured_source_metadata_flow_into_decision():
    p=SourcePermissions(store=True,display=True,model_input=True,export=True)
    text='SEC data\n{"end":"2023-12-31","filed":"2024-02-01","start":"2023-01-01","unit":"USD","val":1}\n'
    sec=source('s',text,p,'sec_companyconcept')
    quote='Revenue is one.'
    claim={'text':quote,'role':'support','citations':[{'source_id':'s','quotation':quote,
        'location':{'character_start':0,'character_end':15}}]}
    analysis=SimpleNamespace(claims=[claim],discovered_requirements=[],facts=[],calculations=[],
                             research_questions=[{'question':'Revenue?', 'claim_indexes':[0]}])
    plan=[{'question':'Revenue?', 'dimensions':{'entities':['SEC_CIK:0000000001'], 'metrics':['us-gaap:Revenues']}}]
    state=build_acquisition_context(question='Revenue?',as_of=None,planned=plan,analysis=analysis,claims=[claim],
                                    sources=[sec],discovered=[],require_counterarguments=False)
    assert state['requirements'][0]['finding_ids'] == state['requirements'][1]['finding_ids'] != []
    assert state['requirements'][1]['dimensions']['entity'] == ['SEC_CIK:0000000001']
    assert state['feedback'][0]['claim_summaries'][0]['text'] == quote
    comparison=compare_requirement_candidates(state['requirements'][1],state['candidates'])[0]
    assert comparison['dimensions']['entity'] == comparison['dimensions']['metric'] == 'match'
    assert comparison['meaning'] == 'metadata_comparison_not_semantic_support'
    assert 'semantic_not_reviewed_or_stale' in state['feedback'][0]['issues']


def test_unrelated_question_is_not_attached_to_original_and_ineligible_text_not_shared():
    p=SourcePermissions(store=True,display=False,model_input=False,export=False)
    hidden=source('hidden','Secret',p)
    claim={'text':'Secret disclosed','role':'support','citations':[{'source_id':'hidden','quotation':'Secret',
        'location':{'character_start':0,'character_end':6}}]}
    analysis=SimpleNamespace(claims=[claim],discovered_requirements=[],facts=[],calculations=[],
                             research_questions=[{'question':'Other?', 'claim_indexes':[0]}])
    state=build_acquisition_context(question='Original?',as_of=None,planned=[{'question':'Other?'}],analysis=analysis,
                                    claims=[claim],sources=[hidden],discovered=[],require_counterarguments=False)
    assert state['requirements'][0]['finding_ids'] == []
    assert state['feedback'][1]['claim_summaries'][0] == {'claim_id': state['requirements'][1]['finding_ids'][0], 'status':'source_access_restricted'}
    assert not state['candidates']


def test_lead_metadata_remains_unverified_and_does_not_expose_query():
    state=build_acquisition_context(question='Q',as_of=None,planned=[],analysis=None,claims=[],sources=[],
        discovered=[{'title':'Lead','url':'https://example.org/report?secret=example',
                     'published_at':'2024-01-01','retrieved_at':'2024-01-02T00:00:00Z',
                     'search_provider':'provider'}],require_counterarguments=False)
    lead=state['candidates'][0]
    assert lead['availability']=='unknown' and lead['metadata']=={}
    assert lead['lead_metadata']['host']=='example.org'
    assert lead['lead_metadata']['verification']=='provider_reported_not_verified'
    assert 'secret' not in str(lead)


def test_parent_summarizes_linked_child_without_claiming_complete_decomposition():
    from app.research_evidence_verifier import review_fingerprint
    p=SourcePermissions(store=True,display=True,model_input=True,export=True)
    s=source('s','Evidence.',p)
    claim={'text':'Evidence.','role':'support','citations':[{'source_id':'s','quotation':'Evidence.',
        'location':{'character_start':0,'character_end':9}}]}
    claim['support_review']={'claim_fingerprint':review_fingerprint(claim,{'s':s.content_sha256},[claim]),'verdict':'supports'}
    analysis=SimpleNamespace(claims=[claim],discovered_requirements=[],facts=[],calculations=[],
                             research_questions=[{'question':'Subquestion?', 'claim_indexes':[0]}])
    state=build_acquisition_context(question='Broad question?',as_of=None,planned=[{'question':'Subquestion?'}],
        analysis=analysis,claims=[claim],sources=[s],discovered=[],require_counterarguments=False)
    parent=state['feedback'][0]
    assert 'no_linked_claim' not in parent['issues']
    assert parent['decomposition_status']=='model_suggested_not_independently_verified'
    assert parent['semantic_status']=='child_model_opinions_not_independent'


def test_decision_context_does_not_drop_authorized_sources_after_analysis_batch():
    p=SourcePermissions(store=True,display=True,model_input=True,export=True)
    sources=[source(f's{i}',f'Text {i}',p) for i in range(21)]
    state=build_acquisition_context(question='Q',as_of=None,planned=[],analysis=None,claims=[],sources=sources,
                                    discovered=[],require_counterarguments=False)
    assert len(state['candidates'])==21 and 's20' in state['source_hashes']
