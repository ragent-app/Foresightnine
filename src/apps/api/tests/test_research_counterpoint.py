import json

from app.research_counterpoint import prepare_counterpoint_prompt


def test_counterpoint_prompt_preserves_sources_question_and_cutoff():
    data = {'question':'收入为何下降？', 'research_cutoff':'2024-01-01T00:00:00+00:00',
            'sources':[{'source_id':'s','text':'Revenue fell. Services rose.','source_coverage':'partial'}],
            'output_schema':{'claims':[]}, 'evidence_guidance':'Old guidance.'}
    base = json.dumps(data,ensure_ascii=False,separators=(',',':'))
    result = prepare_counterpoint_prompt(base, [{'text':'Revenue fell.'}], 2000)
    assert result is not None
    candidate=json.loads(result.prompt)
    assert candidate['sources']==data['sources'] and candidate['question']==data['question']
    assert candidate['research_cutoff']==data['research_cutoff']
    assert candidate['previous_findings_untrusted']==['Revenue fell.']
    assert 'untrusted' in candidate['counterpoint_request']
    assert set(candidate['output_schema'])=={'counterarguments','limitations'}
    assert result.prior_findings_included==1 and not result.truncated_findings
    assert len(result.prompt_sha256)==64


def test_counterpoint_prompt_fails_closed_on_budget_or_invalid_input():
    base=json.dumps({'question':'Q','sources':[{'source_id':'s','text':'原文'*100}]},ensure_ascii=False)
    assert prepare_counterpoint_prompt(base,[{'text':'Conclusion'}],100) is None
    assert prepare_counterpoint_prompt(base,[],5000) is None
    assert prepare_counterpoint_prompt('{bad',[{'text':'x'}],5000) is None
    assert prepare_counterpoint_prompt(json.dumps({'question':'Q','sources':[]}),[{'text':'x'}],5000) is None
    assert prepare_counterpoint_prompt(base,[{'text':'x'}],True) is None


def test_counterpoint_prompt_marks_truncation_and_does_not_mutate_inputs():
    raw={'question':'Q','sources':[{'source_id':'s','text':'Evidence'}]}
    findings=[{'text':'A'*300},{'text':'B'*300},{'text':'C'}]
    result=prepare_counterpoint_prompt(json.dumps(raw),findings,5000)
    assert result is not None and result.truncated_findings and result.prior_findings_included==2
    payload=json.loads(result.prompt)
    assert 'truncated' in payload['counterpoint_request']
    assert payload['previous_findings_untrusted']==['A'*240,'B'*240]
    assert len(findings)==3 and raw['sources'][0]['text']=='Evidence'
