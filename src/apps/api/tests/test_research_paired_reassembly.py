"""Offline reassembly of frozen experiment results; these tests never invoke a model."""
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

from app.research_delivery import assess, assemble_parts
from app.research_partial_answer import merge_checkpoint_parts
from app.research_sources import ResearchSource, SourcePermissions

ROOT = Path(__file__).resolve().parents[3] / 'docs/product/research-delivery-acceptance'
spec = importlib.util.spec_from_file_location('paired_fixture', ROOT / 'paired.py')
paired = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paired)


def source(text, allowed=True):
    return ResearchSource(source_id='exhibit', owner_id='u', title='Independent exercise', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, display=True, model_input=allowed, export=True))


def assemble(question, src, state, claims=()):
    sources = {'exhibit': src}
    eligible = {'exhibit'} if src.permissions.model_input else set()
    rows = assess(question, state, sources, claims=claims)
    return merge_checkpoint_parts(question, state, eligible, assemble_parts(question, state, sources, eligible, rows, list(claims)), sources)


def test_four_frozen_facts_runs_reassemble_once_per_question_without_changing_original_status():
    text, question = paired.CASES['facts']
    for run in ('facts-1-A', 'facts-1-B', 'facts-2-A', 'facts-2-B'):
        result = json.loads((ROOT / 'paired-results' / f'{run}.json').read_text())
        state = {'facts': result['facts'], 'calculations': result['calculations'],
                 'read_ranges': [{'source_id': 'exhibit', 'start': 0, 'end': len(text)}]}
        parts = assemble(question, source(text), state, result['claims'])
        assert len(parts) == 3 and all(part['status'] == 'answered' for part in parts)
        assert len(parts[0]['references']) == 2
        assert '120' in parts[0]['answer'] and '100' in parts[0]['answer']
        assert '20.00' in parts[1]['answer'] and len(parts[1]['references']) == 3
        assert len(parts[2]['references']) == 2
        assert result['status'] == 'partial'  # Reassembly does not rewrite the frozen run.


def test_cross_example_requires_matching_entity_period_permissions_and_deduplicates_punctuation():
    text = 'Atlas FY2023 revenue was USD 80 million. Atlas FY2024 revenue was USD 100 million.'
    f = []
    for period, value in [('FY2024', '100'), ('FY2023', '80')]:
        quote = f'Atlas {period} revenue was USD {value} million.'
        start = text.index(quote)
        f.append({'fact_id': period, 'label': 'Atlas revenue', 'value': value, 'unit': 'USD million',
                  'currency': 'USD', 'period': period, 'source_id': 'exhibit', 'quotation': quote,
                  'character_start': start, 'character_end': start + len(quote)})
    from app.research_calculations import CalculationRequest, calculate
    keys = ('fact_id', 'value', 'unit', 'currency', 'period')
    calc = {**calculate(CalculationRequest.model_validate({'formula': 'growth_percent',
             'first': {k: f[0][k] for k in keys}, 'second': {k: f[1][k] for k in keys}})), 'calculation_id': 'c'}
    q = 'Record both Atlas annual revenues, calculate FY2024 growth, and cite the evidence.'
    state = {'facts': f, 'calculations': [calc], 'read_ranges': [{'source_id': 'exhibit', 'start': 0, 'end': len(text)}],
             'question_parts': [{'question': 'cite the evidence.', 'status': 'unresolved', 'answer': '', 'reason': 'pending', 'references': []}]}
    parts = assemble(q, source(text), state)
    assert len(parts) == 3 and all(part['status'] == 'answered' for part in parts)
    caps = assemble(q.replace('annual revenues', 'Annual Revenues'), source(text), state)
    assert all(part['status'] == 'answered' for part in caps)
    assert not any(part['status'] == 'answered' for part in assemble(q, source(text, False), state))
    wrong_company = assemble(q.replace('Atlas', 'Beta'), source(text), state)
    by_question = {part['question'].rstrip('.'): part for part in wrong_company}
    assert by_question['Record both Beta annual revenues']['status'] != 'answered'
    assert by_question['calculate FY2024 growth']['status'] != 'answered'
    assert by_question['cite the evidence']['status'] != 'answered'
    wrong_year = assemble(q.replace('FY2024', 'FY2022'), source(text), state)
    assert next(part for part in wrong_year if part['question'] == 'calculate FY2022 growth')['status'] != 'answered'
