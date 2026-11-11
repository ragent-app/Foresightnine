import pytest
from app import main
from .test_phase4_live import setup_live, submit, valid_draft

@pytest.mark.parametrize('title', ['利率维持高位，前提证伪','The hypothesis is false','The premise does not hold'])
def test_rejects_premise_failure_inside_conditional_branch(monkeypatch,title):
    draft=valid_draft();draft['branches'][0]['title']=title
    cid,calls,_=setup_live(monkeypatch,draft)
    response=submit(cid)
    assert response.status_code==500
    assert not main.WORLDLINES and len(calls)==1

@pytest.mark.parametrize('location',['event','indicator','state_change','reason','impact'])
def test_rejects_explicit_premise_failure_outside_title(monkeypatch,location):
    draft=valid_draft();branch=draft['branches'][1]
    if location=='reason':branch['reasons']=['The premise does not hold']
    elif location=='impact':branch['impacts'][0]['reason']='前提不成立'
    else:branch['events'][1][location]='前提不成立'
    cid,_,_=setup_live(monkeypatch,draft)
    assert submit(cid).status_code==500 and not main.WORLDLINES

@pytest.mark.parametrize('status',[False,'false','unconditional',None])
def test_requires_explicit_premise_true_relation(monkeypatch,status):
    draft=valid_draft();draft['branches'][0]['premise_status']=status
    cid,_,_=setup_live(monkeypatch,draft)
    assert submit(cid).status_code==500 and not main.WORLDLINES


def test_falsifying_conditions_are_kept_outside_branch_outcomes(monkeypatch):
    draft=valid_draft();draft['branches'][0]['invalidation_conditions']=['前提不成立时停止使用该路径']
    cid,calls,_=setup_live(monkeypatch,draft)
    result=submit(cid)
    assert result.status_code==201
    worldline=result.json()['bundle']['worldlines'][0]
    assert 'premise_guard=v2' in worldline['provenance']
    assert worldline['invalidation_conditions']==['前提不成立时停止使用该路径']
    import json
    payload=json.loads(calls[0][3]);prompt=json.loads(payload['messages'][1]['content'])
    assert prompt['prompt_version']=='scenario-v2'
    assert any('ASSUME' in rule and 'TRUE' in rule for rule in prompt['rules'])
    assert any('hypothetical checkpoint' in rule for rule in prompt['rules'])
    assert payload['max_tokens']==4000  # The generation budget did not change.


def test_runtime_and_healthcheck_use_preinstalled_dependencies():
    from pathlib import Path
    from .helpers import WORKSPACE_ROOT
    dockerfile=(WORKSPACE_ROOT/'infra/docker/Dockerfile.api').read_text()
    compose=(WORKSPACE_ROOT/'infra/docker/compose.yaml').read_text()
    assert 'CMD ["/app/apps/api/.venv/bin/python", "-m", "uvicorn"' in dockerfile
    assert 'uv run --project /app/apps/api' not in compose
    assert '/app/apps/api/.venv/bin/python -c' in compose
