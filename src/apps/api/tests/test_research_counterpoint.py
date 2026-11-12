import json

from app.research_counterpoint import extract_answer_candidates, filter_counterpoint_rows, prepare_counterpoint_prompt


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


def test_extra_answer_leads_are_bounded_and_not_yet_evidence():
    valid = {'text':'Untrusted assertion', 'citations':[{'source_id':'s','quotation':'Source text'}], 'secret':'discard'}
    payload = {'answer': {'bad': {'text':'x','citations':'invalid'}, **{str(i):valid for i in range(25)}}}
    candidates = extract_answer_candidates(payload)
    assert len(candidates) == 19
    assert all('secret' not in item and item['citations'][0]['quotation'] == 'Source text' for item in candidates)
    assert extract_answer_candidates({'answer':['not a mapping']}) == []


def test_counterpoint_rows_keep_valid_entries_and_count_invalid_without_promoting_them():
    valid = {'text':'Costs increased.', 'citations':[{'source_id':'s','quotation':'Costs increased.'}], 'status':'unverified'}
    result = filter_counterpoint_rows([valid, {'text':'secret', 'citations':'bad'}, None])
    assert result is not None
    rows, omitted = result
    assert omitted == 2 and len(rows) == 1 and rows[0]['text'] == 'Costs increased.'
    assert 'status' not in rows[0] and 'secret' not in str(rows)
    assert filter_counterpoint_rows([valid] * 21) is None
    assert filter_counterpoint_rows({'text':'not a list'}) is None
