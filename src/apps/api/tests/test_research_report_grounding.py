from app.research_report_grounding import clause_problem, report_problems


def test_clause_checks_dates_units_entities_and_inference_without_claiming_semantic_verification():
    quotes = ['QQQ closed at 400 USD on 2025-01-02.']
    assert clause_problem('QQQ closed at 400 USD on 2025-01-02.', quotes, [], []) is None
    assert clause_problem('QQQ closed at 400 USD on 2025-01-03.', quotes, [], [])
    assert clause_problem('Revenue grew to 133.1B.', ['Revenue was 133.1 billion USD.'], [], []) is None
    assert clause_problem('Revenue grew to 133.1B.', ['Revenue was 133.1 million USD.'], [], [])
    assert clause_problem('QQQ上涨400%。', quotes, [], [])
    assert clause_problem('BlackRock held QQQ at 400 USD.', quotes, [], [])
    assert clause_problem('QQQ caused revenue to rise.', quotes, [], [])
    assert clause_problem('QQQ may have caused revenue to rise.', quotes, [], []) is None


def test_return_comparison_requires_comparable_context():
    base = {'start_date': '2025-01-02', 'end_date': '2025-12-31', 'entry_type': 'close',
            'price_basis': 'adjusted', 'identity_verified': True, 'start_price_type': 'close', 'end_price_type': 'close'}
    text = 'QQQ outperformed SPY by 5%.'
    quote = 'QQQ and SPY returns differed by 5%.'
    assert clause_problem(text, [quote], [], [{'return_context': {**base, 'instrument': 'QQQ'}, 'value': '10'}, {'return_context': {**base, 'instrument': 'SPY'}, 'value': '5'}]) is None
    assert clause_problem(text, [quote], [], [{'return_context': base}, {'return_context': {**base, 'start_date': '2025-01-03'}}])
    assert clause_problem(text, [quote], [], [{'return_context': base}, {'return_context': {**base, 'identity_verified': False}}])


def test_report_checks_quantitative_claims_per_clause_and_keeps_qualitative_prose():
    claims = [{'text': 'QQQ closed at 400 USD on 2025-01-02.', 'citations': [{'quotation': 'QQQ closed at 400 USD on 2025-01-02.'}]},
              {'text': 'SPY closed at 500 USD on 2025-01-03.', 'citations': [{'quotation': 'SPY closed at 500 USD on 2025-01-03.'}]}]
    report = '## Summary\n\nQQQ closed at 400 USD on 2025-01-02. SPY closed at 500 USD on 2025-01-03.\nThe observations warrant further review.'
    assert report_problems(report, claims) == []
    assert report_problems('QQQ closed at 400 USD on 2025-01-03.', claims)
    assert report_problems('BlackRock held QQQ.', claims)
    same_instrument = [claims[0], {'text': 'QQQ closed at 500 USD on 2025-01-03.', 'citations': [{'quotation': 'QQQ closed at 500 USD on 2025-01-03.'}]}]
    assert report_problems('QQQ closed at 400 USD on 2025-01-03.', same_instrument)
    assert clause_problem('收益率是5%。', ['Return was 5 percent.'], [], []) is None
    assert clause_problem('Price was $400.', ['Price was 400 USD.'], [], []) is None


def test_historical_question_keeps_three_distinct_requested_parts_through_delivery():
    from app.research_delivery import assemble_parts
    from app.research_partial_answer import merge_checkpoint_parts
    question = '分析一下对投资的建议，包括购入的风险以及预期入手时间和持有时间的预估'
    parts = merge_checkpoint_parts(question, {}, set(), assemble_parts(question, {}, {}, set(), [], []))
    assert [part['question'] for part in parts] == ['购入的风险', '预期入手时间', '持有时间的预估']
    question = '分析和平公司和国际和平组织的资料'
    parts = merge_checkpoint_parts(question, {}, set(), assemble_parts(question, {}, {}, set(), [], []))
    assert len(parts) == 1


def test_changed_source_hides_previously_checkpointed_answer_even_if_assessment_is_satisfied():
    from types import SimpleNamespace
    from app.research_partial_answer import merge_checkpoint_parts
    fact = {'fact_id': 'f', 'source_id': 's', 'character_start': 0, 'character_end': 4, 'quotation': '100.'}
    state = {'facts': [fact], 'question_parts': [{'question': 'Revenue', 'status': 'answered', 'answer': '100.',
                                                  'reason': '', 'references': [{'fact_id': 'f', 'origin': 'current'}]}]}
    assessed = [{'id': 'req-1', 'question': 'Revenue', 'status': 'answered', 'answer': '100.',
                 'reason': '', 'references': [{'fact_id': 'f', 'origin': 'current'}], 'core': True}]
    rows = merge_checkpoint_parts('Revenue?', state, {'s'}, assessed, {'s': SimpleNamespace(text='999.')})
    assert rows[0]['id'] == 'req-1' and rows[0]['status'] == 'unresolved'
    assert rows[0]['answer'] == '' and rows[0]['references'] == []


def test_same_day_open_to_close_comparison_requires_matching_price_types():
    base = {'start_date': '2026-11-19', 'end_date': '2026-11-19', 'entry_type': 'open',
            'price_basis': 'unadjusted', 'identity_verified': True, 'start_price_type': 'open', 'end_price_type': 'close'}
    left = {'return_context': {**base, 'instrument': 'ANT'}, 'value': '-12.85'}
    right = {'return_context': {**base, 'instrument': 'QQQ'}, 'value': '-3.88'}
    text = 'ANT underperformed QQQ by 8.97%.'
    assert clause_problem(text, ['ANT and QQQ differed by 8.97%.'], [], [left, right]) is None
    assert clause_problem('ANT outperformed QQQ by 8.97%.', ['ANT and QQQ differed by 8.97%.'], [], [left, right])
    assert clause_problem('ANT underperformed QQQ by 8%.', ['ANT and QQQ differed by 8%.'], [], [left, right])
    right['return_context'] = {**right['return_context'], 'start_price_type': 'close'}
    assert clause_problem(text, ['ANT and QQQ differed by 8.97%.'], [], [left, right])


def test_structured_filing_identity_is_required_when_issuer_absent_from_quote():
    from datetime import datetime, timezone
    from app.research_evidence_requirements import assess
    from app.research_sources import ResearchSource, SourcePermissions
    text = 'Total revenue was $130,497 million.'
    src = ResearchSource(source_id='s', owner_id='u', title='NVDA 10-K', text=text, kind='official',
                         obtained_at=datetime.now(timezone.utc), content_sha256='hash', retrieval_method='sec_filing_html',
                         permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    claim = {'claim_id': 'c', 'role': 'support', 'text': text, 'citations': [{'source_id': 's', 'quotation': text}]}
    state = {'read_ranges': [{'source_id': 's', 'start': 0, 'end': len(text)}]}
    assert assess('NVDA revenue?', state, {'s': src}, claims=[claim])[0]['status'] == 'missing'
    src.provenance = {'symbol': 'NVDA', 'cik': '1045810'}
    assert assess('NVDA revenue?', state, {'s': src}, claims=[claim])[0]['status'] == 'satisfied'
    src.provenance = {'symbol': 'OTHER', 'cik': '1045810'}
    assert assess('NVDA revenue?', state, {'s': src}, claims=[claim])[0]['status'] == 'missing'
