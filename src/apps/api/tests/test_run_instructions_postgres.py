"""Opt-in isolated PostgreSQL integration tests; never use a production DSN."""
import multiprocessing
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.research_run_instructions import ResearchRunInstructions, InstructionConflict
from app.storage import PostgresDocumentRepository, StorageRuntimeError


def _race(dsn, barrier, queue, kind):
    store = ResearchRunInstructions(PostgresDocumentRepository(dsn, 'turns'))
    barrier.wait()
    try:
        if kind == 'append':
            result = store.append('a', 't', 'r', 'new instruction', 'key')[1]
        elif kind == 'seal':
            result = store.seal('a', 't', 'r', 0, operation_id='op')
        elif kind == 'claim':
            result = store.claim_action('a', 't', 'r', 'op', 'call', 0)
        elif kind == 'answer':
            result = store.append('a', 't', 'r', 'next June', 'answer-key')[1]
        elif kind == 'wait':
            result = store.wait_for_user('a', 't', 'r', 'op', 0,
                                         {'question': 'When?', 'why_it_matters': 'Liquidity', 'requirement_id': 'r1'})
        elif kind == 'resume':
            result = store.begin('a', 't', 'r', 'op2', previous_operation_id='op',
                                 allow_reopen=True, require_waiting=True)['status']
        else:
            result = store.stop('a', 't', 'r')['status']
        queue.put(result)
    except InstructionConflict:
        queue.put(False)


@pytest.fixture
def pg_dsn():
    base = os.environ.get('FORESIGHTNINE_TEST_ISOLATED_POSTGRES_DSN')
    if not base:
        pytest.skip('requires an explicitly isolated PostgreSQL test instance')
    import psycopg
    from psycopg import sql
    schema = 'instruction_test_' + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    dsn = psycopg.conninfo.make_conninfo(base, options='-csearch_path=' + schema)
    migration = Path(__file__).resolve().parents[3] / 'infra/migrations/001_research_state.sql'
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(migration.read_text())
    yield dsn
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def _pair(dsn, one, two):
    ctx = multiprocessing.get_context('spawn')
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_race, args=(dsn, barrier, queue, kind)) for kind in (one, two)]
    for process in processes:
        process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    return results


def test_cross_process_serialization_and_transaction_rollback(pg_dsn):
    repo = PostgresDocumentRepository(pg_dsn, 'turns')
    store = ResearchRunInstructions(repo)
    store.begin('a', 't', 'r', 'op')
    results = _pair(pg_dsn, 'append', 'seal')
    assert sorted(results) == [False, True]
    doc = store.read('a', 't', 'r')
    assert (doc['status'] == 'sealed') != bool(doc['instructions'])

    store.begin('a', 't', 'r2', 'op')
    other_repo = PostgresDocumentRepository(pg_dsn, 'outputs')
    def fail():
        other_repo['version'] = {'text': 'must roll back'}
        raise ValueError('simulate version write failure')
    with pytest.raises(StorageRuntimeError):
        store.seal('a', 't', 'r2', 0, operation_id='op', persist=fail)
    assert other_repo.get('version') is None
    assert store.read('a', 't', 'r2')['status'] == 'open'


def test_cross_process_stop_fences_unstarted_action(pg_dsn):
    store = ResearchRunInstructions(PostgresDocumentRepository(pg_dsn, 'turns'))
    store.begin('a', 't', 'r', 'op')
    results = _pair(pg_dsn, 'stop', 'claim')
    assert 'stopped' in results
    assert not store.claim_action('a', 't', 'r', 'op', 'another-call', 0)
    # If claim won the race, that call was already started; it is never replayed.
    assert not store.claim_action('a', 't', 'r', 'op', 'call', 0)


def test_cross_process_wait_and_answer_keep_the_new_instruction(pg_dsn):
    store = ResearchRunInstructions(PostgresDocumentRepository(pg_dsn, 'turns'))
    store.begin('a', 't', 'r', 'op')
    _pair(pg_dsn, 'wait', 'answer')
    doc = store.read('a', 't', 'r')
    assert doc['sequence'] == 1 and doc['instructions'][0]['text'] == 'next June'
    assert doc['status'] in ('open', 'awaiting_user')
    if doc['status'] == 'awaiting_user':
        with pytest.raises(InstructionConflict):
            store.accept_findings('a', 't', 'r', 'op', 'v1')
        resumed = store.begin('a', 't', 'r', 'op2', previous_operation_id='op', allow_reopen=True)
        assert resumed['status'] == 'open' and resumed['sequence'] == 1


def test_cross_process_stop_and_answer_never_reopen_stopped_turn(pg_dsn):
    store = ResearchRunInstructions(PostgresDocumentRepository(pg_dsn, 'turns'))
    store.begin('a', 't', 'r', 'op')
    assert store.wait_for_user('a', 't', 'r', 'op', 0, {'question': 'When?'})
    result = _pair(pg_dsn, 'stop', 'answer')
    assert 'stopped' in result
    doc = store.read('a', 't', 'r')
    assert doc['status'] == 'stopped' and doc['sequence'] in (0, 1)
    with pytest.raises(InstructionConflict):
        store.fenced('a', 't', 'r', 'op', lambda: None, allow_waiting=True)
    with pytest.raises(InstructionConflict):
        store.begin('a', 't', 'r', 'op2', previous_operation_id='op', allow_reopen=True, require_waiting=True)


def test_cross_process_stop_and_resume_leave_turn_stopped(pg_dsn):
    store = ResearchRunInstructions(PostgresDocumentRepository(pg_dsn, 'turns'))
    store.begin('a', 't', 'r', 'op')
    assert store.wait_for_user('a', 't', 'r', 'op', 0, {'question': 'When?'})
    assert 'stopped' in _pair(pg_dsn, 'stop', 'resume')
    doc = store.read('a', 't', 'r')
    assert doc['status'] == 'stopped'
    assert doc['operation_id'] in ('op', 'op2')


def test_duplicate_answer_receipt_and_wait_checkpoint_are_durable(pg_dsn):
    store = ResearchRunInstructions(PostgresDocumentRepository(pg_dsn, 'turns'))
    store.begin('a', 't', 'r', 'op')
    assert store.wait_for_user('a', 't', 'r', 'op', 0, {'question': 'When?'})
    assert sorted(_pair(pg_dsn, 'answer', 'answer')) == [False, True]
    reopened = ResearchRunInstructions(PostgresDocumentRepository(pg_dsn, 'turns'))
    doc = reopened.read('a', 't', 'r')
    assert doc['status'] == 'awaiting_user' and doc['sequence'] == 1
    assert reopened.append('a', 't', 'r', 'next June', 'answer-key')[1] is False
    assert reopened.begin('a', 't', 'r', 'op2', previous_operation_id='op', allow_reopen=True)['sequence'] == 1


def test_wait_checkpoint_rolls_back_with_candidate_write(pg_dsn):
    store = ResearchRunInstructions(PostgresDocumentRepository(pg_dsn, 'turns'))
    result_repo = PostgresDocumentRepository(pg_dsn, 'outputs')
    store.begin('a', 't', 'r', 'op')
    def fail():
        result_repo['candidate'] = {'report': 'partial'}
        raise ValueError('simulated persistence failure')
    with pytest.raises(StorageRuntimeError):
        store.wait_for_user('a', 't', 'r', 'op', 0, {'question': 'When?'}, persist=fail)
    assert result_repo.get('candidate') is None
    assert store.read('a', 't', 'r')['status'] == 'open'
