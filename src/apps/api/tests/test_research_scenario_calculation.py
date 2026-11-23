import pytest
from pydantic import ValidationError

from app.research_scenario_calculation import ScenarioCalculation, scenario_receipt, scenario_receipt_intact


def example():
    question = '本月支出 5000 元 CNY，本月收入 3200 元 CNY。'
    text = '假设本月收入 3200 元 CNY，短缺为 1800.00 元 CNY。'
    data = {'formula': 'difference', 'operands': [
        {'label': '支出', 'value': '5000', 'unit': '元', 'currency': 'CNY', 'period': '本月', 'kind': 'user', 'sequence': 0, 'quotation': '本月支出 5000 元 CNY'},
        {'label': '收入', 'value': '3200', 'unit': '元', 'currency': 'CNY', 'period': '本月', 'kind': 'assumption', 'quotation': '假设本月收入 3200 元 CNY'},
    ]}
    return question, text, data


def test_shortfall_receipt_recalculates_and_binds_messages_and_result():
    q, text, data = example()
    receipt = scenario_receipt(ScenarioCalculation.model_validate(data), text, {0: q})
    assert receipt.value == '1800.00'
    assert receipt.expression == 'first - second'
    assert scenario_receipt_intact(receipt, text, {0: q})
    assert not scenario_receipt_intact(receipt.model_copy(update={'value': '1700.00'}), text, {0: q})
    assert not scenario_receipt_intact(receipt, text, {0: q + '变更'})
    assert not scenario_receipt_intact(receipt, text.replace('1800.00', '1700.00'), {0: q})


@pytest.mark.parametrize('mutation', ['value', 'quote', 'currency', 'period', 'result', 'assumption_label'])
def test_invalid_or_incompatible_inputs_rejected(mutation):
    q, text, data = example()
    if mutation == 'value': data['operands'][0]['value'] = '500'
    if mutation == 'quote': data['operands'][0]['quotation'] = '本月支出 5001 元 CNY'
    if mutation == 'currency':
        data['operands'][1]['currency'] = 'USD'
        data['operands'][1]['quotation'] = '假设本月收入 3200 元 USD'
        text = text.replace('3200 元 CNY', '3200 元 USD')
    if mutation == 'period':
        data['operands'][1]['period'] = '下月'
        data['operands'][1]['quotation'] = '假设下月收入 3200 元 CNY'
        text = text.replace('假设本月收入', '假设下月收入')
    if mutation == 'result': text = text.replace('1800.00', '1700.00')
    if mutation == 'assumption_label': data['operands'][1]['quotation'] = '本月收入 3200 元 CNY'
    with pytest.raises((ValueError, ValidationError)):
        scenario_receipt(ScenarioCalculation.model_validate(data), text, {0: q})


def test_appended_user_message_is_bound():
    q, text, data = example()
    data['operands'][1].update(kind='user', sequence=1, quotation='本月收入 3200 元 CNY')
    receipt = scenario_receipt(ScenarioCalculation.model_validate(data), text, {0: q, 1: '本月收入 3200 元 CNY'})
    assert scenario_receipt_intact(receipt, text, {0: q, 1: '本月收入 3200 元 CNY'})
    assert not scenario_receipt_intact(receipt, text, {0: q})


def test_submit_requires_conditional_review_and_persists_receipt():
    import json
    from app.model_protocol import ModelResponse, ModelUsage
    from app.research_runtime import run_research_agent
    from tests.test_research_runtime_attention import _ctx, _tool_response, NativeTurnRecorder
    q, text, data = example()
    submission = {'report': text, 'claims': [], 'source_free_answers': [
        {'basis': 'assumption', 'text': text, 'scenario_calculation': data}],
        'limitations': [], 'stopping_reason': 'answered'}
    def review(turn, _):
        items = json.loads(turn.user)['candidates']
        reviews = [{**{k: item[k] for k in ('requirement_id', 'claim_id', 'review_key')},
                    'evidence_ids': [str(i + 1) for i in range(len(item['evidence_context']))],
                    'verdict': 'conditional', 'condition': '假设本月收入 3200 元 CNY', 'reason': '假设下的差额。'} for item in items]
        return ModelResponse({'content': json.dumps({'reviews': reviews}, ensure_ascii=False)}, (), ModelUsage(5, 3, 8), 'stop')
    saved = []
    class Model(NativeTurnRecorder):
        def complete_turn(self, turn, **kwargs):
            if not turn.tools and 'candidates' in json.loads(turn.user):
                return review(turn, 0)
            return super().complete_turn(turn, **kwargs)
    model = Model([_tool_response([('submit_research', submission, 'submit')])])
    result = run_research_agent(question=q, sources=[], model=model, context=_ctx(), checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    assert result.status == 'completed'
    assert result.claims[0]['scenario_calculation']['value'] == '1800.00'
    assert any(r['verdict'] == 'conditional' for r in saved[-1]['_semantic_reviews'])


@pytest.mark.parametrize('wrong', ['-1800.00 元 CNY', '11800.00 元 CNY', '1800.00 元 CNYX'])
def test_result_numeric_and_currency_boundaries(wrong):
    q, text, data = example()
    with pytest.raises(ValueError):
        scenario_receipt(ScenarioCalculation.model_validate(data), text.replace('1800.00 元 CNY', wrong), {0: q})


def test_user_version_cannot_supply_calculation_receipt(monkeypatch):
    from tests.test_research_topics import start, save
    q, text, data = example()
    receipt = scenario_receipt(ScenarioCalculation.model_validate(data), text, {0: q}).model_dump(mode='json')
    client, headers, _, topic, path = start(monkeypatch, 'Source.', 'scenario-edit')
    submitted = {'claims': [{'text': text, 'role': 'support', 'scenario_calculation': receipt}]}
    created = save(client, headers, path, topic['versions'][0], submitted)
    assert created.status_code == 201
    version = created.json()['versions'][-1]
    assert version['content']['claims'][0]['scenario_calculation'] is None
    changed = version['content']
    changed['claims'][0]['text'] += ' edited'
    changed['claims'][0]['scenario_calculation'] = receipt
    edited = save(client, headers, path, version, changed)
    assert edited.status_code == 201
    claim = edited.json()['versions'][-1]['content']['claims'][0]
    assert claim['edited_by_user'] and claim['scenario_calculation'] is None
