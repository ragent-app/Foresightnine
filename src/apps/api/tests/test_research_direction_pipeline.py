"""Provenance-bound directional conversion and deterministic calculation."""
from datetime import datetime, timezone
from hashlib import sha256
from copy import deepcopy

from app.research_analysis import analyze
from app.research_calculations import derive_direction_proof, verified_direction_proof
from app.research_sources import ResearchSource, SourcePermissions


def direction_fixture(text, quotation, *, label='Output', period='2024', value='-2.5', unit='percent'):
    start = text.index(quotation)
    fact = {'fact_id': 'f', 'label': label, 'value': value, 'unit': unit, 'currency': None,
            'period': period, 'source_id': 's', 'quotation': quotation,
            'location': {'character_start': start, 'character_end': start + len(quotation)}}
    return fact, sha256(text.encode()).hexdigest()


def test_direction_derivation_requires_unique_subject_measure_period_and_source():
    text = 'In 2024, output declined 2.5 percent.'
    fact, digest = direction_fixture(text, text)
    proof = derive_direction_proof(fact, text, digest, 0, len(text))
    assert proof and proof['direction'] == 'decrease' and proof['context_start'] == 0
    fact['direction_derivation'] = proof
    assert verified_direction_proof(fact)
    for changes in ({'label': 'Input'}, {'label': 'Output margin'}, {'period': '2023'},
                    {'value': '-3'}, {'unit': 'percentage points'}, {'currency': 'USD'}):
        changed = {**fact, **changes}
        assert not verified_direction_proof(changed)
        assert derive_direction_proof(changed, text, digest, 0, len(text)) is None
    for sentence in ('In 2024, output rose 2.5 percent.',
                     'In 2024, output did not decline 2.5 percent.',
                     'In 2024, output declined to 2.5 percent.',
                     'In 2024, output declined 2.5 percent and rose 1 percent.',
                     'In 2024, output declined 2.5 percent and costs fell 1 percent.'):
        candidate, h = direction_fixture(sentence, sentence)
        assert derive_direction_proof(candidate, sentence, h, 0, len(sentence)) is None
    assert derive_direction_proof(fact, text, '0' * 64, 0, len(text)) is None
    duplicate = text + '\n\n' + text
    assert derive_direction_proof(fact, duplicate, sha256(duplicate.encode()).hexdigest(), 0, len(duplicate)) is None
    other = 'In the first quarter of 2021, shipments fell 4.1 percent.'
    other_fact, other_digest = direction_fixture(other, other, label='Shipments growth Q1', period='Q1 2021', value='-4.1')
    assert derive_direction_proof(other_fact, other, other_digest, 0, len(other))
    assert not derive_direction_proof({**other_fact, 'period': '2021'}, other, other_digest, 0, len(other))


def test_directional_fact_enters_reproducible_calculation_without_claiming_verification():
    text = 'In 2024, output declined 2.5 percent.\n\nIn 2023, output increased 1.0 percent.'
    report = ResearchSource(source_id='s', owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    raw = {'facts': [
        {'fact_id': 'current', 'label': 'Output', 'value': '-2.5', 'unit': 'percent', 'period': '2024',
         'citation': {'source_id': 's', 'quotation': 'In 2024, output declined 2.5 percent.'}},
        {'fact_id': 'prior', 'label': 'Output', 'value': '1.0', 'unit': 'percent', 'period': '2023',
         'citation': {'source_id': 's', 'quotation': 'In 2023, output increased 1.0 percent.'}}],
        'calculations': [{'formula': 'difference', 'first_fact_id': 'current', 'second_fact_id': 'prior'}]}
    result = analyze('Compare output in 2024 and 2023.', [report], None, lambda _: (deepcopy(raw), 10))
    current = result.facts[0]
    assert current['verification'] == 'model_extraction_not_independently_verified'
    assert verified_direction_proof(current)
    assert result.calculations[0]['value'] == '-3.50'
    assert result.calculations[0]['unit'] == 'percentage points'
    assert result.calculations[0]['input_status'] == 'matched'


def test_existing_explicit_signed_and_parenthesized_values_remain_calculable():
    text = 'Output was -2.5 percent in 2024.\n\nOutput was (1.0) percent in 2023.'
    report = ResearchSource(source_id='s', owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    raw = {'facts': [
        {'fact_id': 'current', 'label': 'Output', 'value': '-2.5', 'unit': 'percent', 'period': '2024',
         'citation': {'source_id': 's', 'quotation': 'Output was -2.5 percent in 2024.'}},
        {'fact_id': 'prior', 'label': 'Output', 'value': '-1.0', 'unit': 'percent', 'period': '2023',
         'citation': {'source_id': 's', 'quotation': 'Output was (1.0) percent in 2023.'}}],
        'calculations': [{'formula': 'difference', 'first_fact_id': 'current', 'second_fact_id': 'prior'}]}
    result = analyze('Compare output in 2024 and 2023.', [report], None, lambda _: (raw, 10))
    assert result.calculations[0]['value'] == '-1.50'
    assert result.calculations[0]['input_status'] == 'matched'


def test_hypothetical_forecast_or_target_never_gets_direction_proof():
    for text in ('In 2024, output declined 2.5 percent if demand weakened.',
                 'In 2024, output declined 2.5 percent in the forecast.',
                 'In 2024, output declined 2.5 percent under the target scenario.',
                 'In 2024, output declined 2.5 percent assuming no intervention.'):
        fact, digest = direction_fixture(text, text)
        assert derive_direction_proof(fact, text, digest, 0, len(text)) is None
        fact, digest = direction_fixture(text, text[:text.index(' percent') + len(' percent')])
        assert derive_direction_proof(fact, text, digest, 0, len(text)) is None
