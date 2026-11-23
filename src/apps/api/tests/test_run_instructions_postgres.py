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
