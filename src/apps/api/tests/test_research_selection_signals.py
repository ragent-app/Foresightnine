"""Offline retrieval-selection regression; model outputs are test doubles."""
from datetime import datetime, timezone
from pathlib import Path
from app.research_analysis import analyze, SYSTEM_PROMPT
from app.research_context import EvidenceUnit, observation_retrieval_signal, period_retrieval_signals
from app.research_coordinator import research_prompt_budget
from app.research_sources import ResearchSource, SourcePermissions
from tests.research_prompt_helpers import decode_research_prompt


def source(sid, text):
    return ResearchSource(source_id=sid,owner_id='test',title='report',text=text,kind='user_supplied',
        obtained_at=datetime(2026,1,1,tzinfo=timezone.utc),content_sha256='test',
        permissions=SourcePermissions(store=True,model_input=True,display=True,export=True))


def run(question, sources, budget=5000, **kwargs):
    captured=[]
    result=analyze(question,sources,None,lambda prompt:(captured.append(prompt) or {},0),max_prompt_chars=budget,output_contract=None,**kwargs)
    return result, captured


def test_period_hints_keep_year_and_report_ambiguity():
    assert period_retrieval_signals('second and third quarters of 2022')[0] == {(2022,2),(2022,3)}
    assert period_retrieval_signals('2023Q2')[0] == {(2023,2)}
    assert period_retrieval_signals('2022年第二季度')[0] == {(2022,2)}
    assert period_retrieval_signals('2022 Q2 and 2023 Q3')[0] == {(2022,2),(2023,3)}
    assert period_retrieval_signals('2022 and 2023 Q3') == (set(),'ambiguous')
    assert period_retrieval_signals('in 2022')[0] == {(2022,0)}
    assert period_retrieval_signals('January 2022')[1] == 'unsupported'
    assert period_retrieval_signals('in the second quarter') == (set(),'ambiguous')


def test_numeric_observation_cues_ignore_identifiers_and_require_relevance():
    assert not observation_retrieval_signal(EvidenceUnit('sentence',((0,'Sales page 2, dated 2022.'),)), {'sales'})
    assert not observation_retrieval_signal(EvidenceUnit('sentence',((0,'Margins were 13 percent.'),)), {'sales'})
    assert observation_retrieval_signal(EvidenceUnit('sentence',((0,'Sales fell 13 percent.'),)), {'sales'})


def test_cross_source_period_selection_respects_budget_and_provenance():
    q='Compare revenue in the second and third quarters of 2021 and explain limitations.'
    filler='\n'.join(f'Revenue risk background note {i} with explanation.' for i in range(100))
    a=source('a',filler+'\nRevenue was 13 percent in the second quarter of 2021.')
    b=source('b',filler+'\nRevenue was 7 percent in the third quarter of 2021.')
    result,captured=run(q,[a,b],budget=4900)
    assert captured and len(captured[0].encode()) <= 4900
    text=' '.join(x['text'] for x in decode_research_prompt(captured[0])['sources'])
    assert '13 percent' in text and '7 percent' in text
    assert not any('Retrieval signals for requested periods' in x for x in result.limitations)


def test_350_snapshot_development_regression_without_model_call():
    path=Path(__file__).resolve().parents[3]/'docs/product/phase-a/337-stage-b-independent-acceptance/snapshots/bea-2022-q3.txt'
    text=path.read_text()
    report=source('bea',text)
    q=('Using the supplied BEA release, report the real GDP growth rates for the second and third quarters of 2022 '
       'and calculate the difference in percentage points. Explain the estimate and period limitations.')
    budget=research_prompt_budget(6000,SYSTEM_PROMPT)
    result,captured=run(q,[report],budget=budget)
    assert captured and len(captured[0].encode())<=budget
    selected=' '.join(text[s['character_start']:s['character_end']] for s in result.segments)
    assert '2.6' in selected and '0.6' in selected


def test_table_header_and_metric_dimensions_are_retrieval_hints():
    q='Compare revenue and margin in 2021Q2 and 2021Q3.'
    text=('Revenue and margin context.\n'*90 +
          '| Metric | 2021Q2 | 2021Q3 |\n| --- | --- | --- |\n| Revenue | 12 percent | 12 percent |\n')
    requirements=({'question':q,'dimensions':{'entities':['Company Delta'],'metrics':['revenue','margin'],
                   'periods':['2021Q2','2021Q3']}},)
    result,captured=run(q,[source('table',text)],budget=4900,planning_requirements=requirements)
    assert captured and len(captured[0].encode())<=4900
    assert any('entity or metric' in item for item in result.limitations)


def test_ambiguous_period_is_reported_without_asserting_absence():
    result,captured=run('Compare sales in the second quarter.',[source('s','Sales reached 8 percent in the second quarter.')])
    assert captured and any('period coverage is unknown' in x for x in result.limitations)


def test_cross_year_and_same_value_observations_remain_distinct():
    q='Compare revenue in 2022Q4 and 2023Q1.'
    filler='\n'.join(f'Revenue background {i} and explanation.' for i in range(90))
    a=source('a',filler+'\nRevenue was 5 percent in 2022Q4.')
    b=source('b',filler+'\nRevenue was 5 percent in 2023Q1.')
    result,captured=run(q,[a,b],budget=4900)
    assert captured and len(captured[0].encode())<=4900
    selected=' '.join(item['text'] for item in decode_research_prompt(captured[0])['sources'])
    assert '2022Q4' in selected and '2023Q1' in selected
    assert not any('Retrieval signals for requested periods' in x for x in result.limitations)


def test_annual_modal_may_is_not_a_month():
    assert period_retrieval_signals('How may revenue change in 2022 and 2023?')[0] == {(2022,0),(2023,0)}
    assert period_retrieval_signals('monthly revenue in 2023')[1] == 'unsupported'


def test_same_values_with_different_entity_and_metric_are_not_identical_cues():
    a=EvidenceUnit('sentence',((0,'Orchid revenue was 8 percent.'),))
    b=EvidenceUnit('sentence',((0,'Willow margin was 8 percent.'),))
    terms={'orchid','willow','revenue','margin'}
    assert observation_retrieval_signal(a,terms) == observation_retrieval_signal(b,terms)
    # The selection objective includes matched entity/metric terms in addition
    # to values; equal numeric strings alone do not identify an observation.
    q='Compare Orchid revenue and Willow margin.'
    filler='\n'.join(f'Background information {i} about market conditions.' for i in range(60))
    result,captured=run(q,[source('a',filler+'\n'+a.parts[0][1]),source('b',filler+'\n'+b.parts[0][1])],budget=4900)
    assert captured
    selected=' '.join(x['text'] for x in decode_research_prompt(captured[0])['sources'])
    assert a.parts[0][1] in selected and b.parts[0][1] in selected


def test_focus_remains_selected_during_dimension_replacement():
    q='Compare revenue in 2021Q2 and 2021Q3.'
    text=('Background revenue information.\n'*80 + 'Revenue was 4 percent in 2021Q2.\n'
          'Revenue was 7 percent in 2021Q3.\n' + 'Revenue caveat: the estimate remains preliminary.')
    focus_text='Revenue caveat: the estimate remains preliminary.'
    start=text.index(focus_text)
    result,captured=run(q,[source('s',text)],budget=4900,focus=('s',start,start+len(focus_text)))
    assert captured and len(captured[0].encode())<=4900
    assert any(s['character_start'] <= start and s['character_end'] >= start+len(focus_text) for s in result.segments)
