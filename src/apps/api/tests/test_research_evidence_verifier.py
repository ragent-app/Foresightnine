from datetime import datetime, timezone
from types import SimpleNamespace

from app.research_evidence_verifier import compare_requirement_candidates, claim_fingerprint, derive_verification_feedback
from app.research_sources import ResearchSource, SourcePermissions
from app.research_verification import prepare_support_review, apply_support_review


def verified(value, *, complete=True, comparison='canonical_exact'):
    return {'value': value, 'status': 'verified', 'complete': complete, 'comparison': comparison}


def test_source_comparison_is_requirement_specific_without_promoting_leads():
    requirement = {'requirement_id': 'r', 'dimensions': {'entity': {'value':'issuer:1','namespace':'issuer-id'}, 'currency': {'value':'EUR','namespace':'iso4217'}, 'metric': 'revenue'},
                   'required_permissions': ['store', 'model_input'], 'as_of': '2022-12-31T23:59:59Z'}
    base = {'availability': 'available', 'permissions': {'store': True, 'model_input': True},
            'metadata': {'entity': {**verified('issuer:1'), 'namespace':'issuer-id'}, 'currency': {**verified('USD'), 'namespace':'iso4217'}, 'metric': verified('revenue'),
                         'publication_at':'2022-05-01T00:00:00Z', 'publication_status':'verified',
                         'revision_at':'2023-01-01T00:00:00Z', 'revision_status':'verified'}}
    result = compare_requirement_candidates(requirement, [{'candidate_id':'wrong', **base},
        {'candidate_id':'unknown', 'availability':'available','permissions':{'store':True},'metadata':{}}])
    assert result[0]['disposition'] == 'rejected'
    assert result[0]['dimensions']['currency'] == result[0]['dimensions']['revision'] == 'mismatch'
    assert result[0]['dimensions']['metric'] == 'match'
    assert result[0]['meaning'] == 'metadata_comparison_not_semantic_support'
    assert result[1]['disposition'] == 'unknown' and result[1]['dimensions']['permissions'] == 'unknown'
    assert result[0]['dimensions']['independence'] == 'unknown'
    alias = compare_requirement_candidates({'dimensions':{'entity':'Acme Corp'}},
        [{'candidate_id':'a','availability':'available','metadata':{'entity':verified('ACME', comparison='unmapped')}}])[0]
    assert alias['dimensions']['entity'] == 'unknown'
    other_namespace = compare_requirement_candidates({'dimensions':{'entity':{'value':'Acme','namespace':'company-name'}}},
        [{'candidate_id':'b','availability':'available','metadata':{'entity':{**verified('123'), 'namespace':'sec-cik'}}}])[0]
    assert other_namespace['dimensions']['entity'] == 'unknown'


def test_model_review_is_bound_to_claim_and_source_and_feedback_replans():
    source = ResearchSource(source_id='s',owner_id='o',title='Report',text='Revenue grew.',kind='official',
        obtained_at=datetime.now(timezone.utc),content_sha256='a'*64,
        permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
    claim = {'claim_id':'c','text':'Revenue grew.','role':'support',
             'citations':[{'source_id':'s','quotation':'Revenue grew.','location':{'character_start':0,'character_end':13}}]}
    batch = prepare_support_review([claim], [source], 20000)
    reviewed = apply_support_review([claim], batch, {'reviews':[{'claim_index':0,'verdict':'contradicts'}]})
    assert reviewed[0]['support_review']['claim_fingerprint'] == claim_fingerprint(claim, {'s':'a'*64})
    requirements = [{'requirement_id':'r','finding_ids':['c'],'conflict':True,'counterpoint_required':True}]
    links = [{'claim_id':'c','citation_location_status':'all_recorded'}]
    feedback = derive_verification_feedback(requirements, reviewed, links, source_hashes={'s':'a'*64})[0]
    assert feedback['mechanical_status'] == 'location_recorded'
    assert feedback['semantic_status'] == 'model_opinion_not_independent'
    assert 'semantic_contradicts' in feedback['issues']
    assert 'investigate_conflicting_sources' in feedback['next_actions']
    assert 'investigate_counterpoint' in feedback['next_actions']
    changed = [{**reviewed[0], 'text':'Revenue fell.'}]
    stale = derive_verification_feedback(requirements, changed, links, source_hashes={'s':'a'*64})[0]
    assert stale['claim_reviews'][0]['model_opinion'] == 'not_reviewed_or_stale'
    rel_changed = [{**reviewed[0], 'responds_to_claim_ids':['new-claim']}]
    assert derive_verification_feedback(requirements, rel_changed, links, source_hashes={'s':'a'*64})[0]['claim_reviews'][0]['model_opinion'] == 'not_reviewed_or_stale'
    changed_source = derive_verification_feedback(requirements, reviewed, links, source_hashes={'s':'b'*64})[0]
    assert changed_source['claim_reviews'][0]['model_opinion'] == 'not_reviewed_or_stale'
    no_hash = derive_verification_feedback(requirements, reviewed, links)[0]
    assert no_hash['claim_reviews'][0]['model_opinion'] == 'not_reviewed_or_stale'


def test_counterpoint_requires_current_relevant_review_but_not_perpetual_gap():
    from app.research_evidence_verifier import review_fingerprint
    quote={'source_id':'s','quotation':'Evidence.','location':{'character_start':0,'character_end':9}}
    finding={'claim_id':'a','text':'A','role':'support','citations':[quote]}
    counter={'claim_id':'b','text':'B','role':'counterargument','citations':[quote],
             'suggested_responds_to_claim_indexes':[0]}
    claims=[finding,counter]
    for item in claims:
        item['support_review']={'claim_fingerprint':review_fingerprint(item,{'s':'x'},claims),'verdict':'supports',
                                'relevance':'relevant','relevance_context_complete':True}
    requirement={'requirement_id':'r','finding_ids':['a'],'counterpoint_required':True,
                 'counterpoints':[{'claim_id':'b','finding_ids':['a']}]}
    links=[{'claim_id':'a','citation_location_status':'all_recorded'}]
    result=derive_verification_feedback([requirement],claims,links,source_hashes={'s':'x'})[0]
    assert result['issues'] == [] and result['semantic_status']=='model_opinion_not_independent'
    changed=[{**finding,'text':'Changed'},counter]
    result=derive_verification_feedback([requirement],changed,links,source_hashes={'s':'x'})[0]
    assert 'counterpoint_requires_assessment' in result['issues']
