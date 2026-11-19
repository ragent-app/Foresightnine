"""Unit and async API tests for v2 research live SSE stream (L2)."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

import app.main as api
from app.operations import ApiProblem
from app.research_live_stream import ResearchLiveBroker, format_sse_frame, parse_last_event_id
from tests.test_research_multiturn import Q1, Scripted, env, read_all, submit  # noqa: F401


@pytest.fixture
def live_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        api.app,
        host='127.0.0.1',
        port=port,
        log_level='error',
        timeout_graceful_shutdown=0.5,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            break
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        sock.close()
        raise RuntimeError('uvicorn test server failed to start')
    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
        try:
            sock.close()
        except OSError:
            pass


def _parse_sse_frames(raw_lines: list[str]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    for line in raw_lines:
        if line == '':
            if event_name is not None or data_lines:
                payload = '\n'.join(data_lines)
                frames.append({
                    'event': event_name,
                    'id': event_id,
                    'data': json.loads(payload) if payload else None,
                })
                event_name = None
                event_id = None
                data_lines = []
            continue
        if line.startswith(':'):
            frames.append({'comment': line[1:].strip()})
            continue
        if line.startswith('event:'):
            event_name = line[6:].strip()
        elif line.startswith('id:'):
            event_id = line[3:].strip()
        elif line.startswith('data:'):
            data_lines.append(line[5:].strip())
    if event_name is not None or data_lines:
        payload = '\n'.join(data_lines)
        frames.append({
            'event': event_name,
            'id': event_id,
            'data': json.loads(payload) if payload else None,
        })
    return frames


async def _read_sse_frames(response: httpx.Response, *, stop_on_terminal: bool = True) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == '':
            if event_name is not None or data_lines:
                payload = '\n'.join(data_lines)
                frame = {
                    'event': event_name,
                    'id': event_id,
                    'data': json.loads(payload) if payload else None,
                }
                frames.append(frame)
                event_name = None
                event_id = None
                data_lines = []
                if stop_on_terminal and frame['event'] == 'run.terminal':
                    break
            continue
        if line.startswith(':'):
            frames.append({'comment': line[1:].strip()})
            continue
        if line.startswith('event:'):
            event_name = line[6:].strip()
        elif line.startswith('id:'):
            event_id = line[3:].strip()
        elif line.startswith('data:'):
            data_lines.append(line[5:].strip())
    return frames


def test_parse_last_event_id_valid_and_malformed():
    assert parse_last_event_id(None) == (0, False)
    assert parse_last_event_id('') == (0, False)
    assert parse_last_event_id('ev:0') == (1, False)
    assert parse_last_event_id('ev:14') == (15, False)
    assert parse_last_event_id('cp:0') == (0, False)
    assert parse_last_event_id('cp:9') == (9, False)
    assert parse_last_event_id('done:completed') == (0, True)
    assert parse_last_event_id('done:failed') == (0, True)
    assert parse_last_event_id('done:cancelled') == (0, True)

    for bad in ('   ', 'bad', 'ev:-1', 'ev:abc', 'ev:²', 'cp:-1', 'cp:x', 'done:running', 'done:unknown', 'op:1'):
        with pytest.raises(ApiProblem) as exc_info:
            parse_last_event_id(bad)
        assert exc_info.value.status_code == 400
        assert exc_info.value.error_code == 'invalid_last_event_id'


def test_format_sse_frame():
    text = format_sse_frame('step.event', {'a': 1}, event_id='ev:0')
    assert text == 'event: step.event\nid: ev:0\ndata: {"a":1}\n\n'
    no_id = format_sse_frame('step.current', {'b': 2})
    assert no_id == 'event: step.current\ndata: {"b":2}\n\n'


def test_broker_slow_consumer_backpressure_and_grace_cleanup():
    async def scenario():
        now = [100.0]
        broker = ResearchLiveBroker(queue_maxsize=4, grace_seconds=30.0, clock=lambda: now[0])
        loop = asyncio.get_running_loop()
        sub = broker.subscribe('op-bp', account_id='acc-1', loop=loop)

        # Flood queue with transient deltas (should never block or raise).
        for seq in range(10):
            broker.publish_delta('op-bp', call_seq=1, channel='thinking', delta=f'd{seq}', seq=seq)
        await asyncio.sleep(0)
        assert sub.queue.qsize() == 4

        # Publish durable events and terminal while queue is full.
        broker.publish_progress('op-bp', {
            'operation_status': 'running',
            'current': {'kind': 'model', 'phase': 'thinking'},
            'usage': {'model_calls': 1, 'search_calls': 0, 'total_tokens': 5},
            'interim': {'leads_count': 0, 'acquired_sources_count': 0, 'read_sources_count': 0, 'facts_count': 0, 'calculations_count': 0},
            'events': [{'index': 0, 'kind': 'model'}],
            'event_count': 1,
            'updated_at': '2026-11-19T00:00:00Z',
        })
        broker.publish_terminal('op-bp', operation_status='completed', version_id='v2', progress={
            'operation_status': 'completed',
            'current': None,
            'usage': {'model_calls': 1, 'search_calls': 0, 'total_tokens': 5},
            'interim': {'leads_count': 0, 'acquired_sources_count': 0, 'read_sources_count': 0, 'facts_count': 0, 'calculations_count': 0},
            'events': [{'index': 0, 'kind': 'model'}, {'index': 1, 'kind': 'submission', 'status': 'completed'}],
            'event_count': 2,
            'updated_at': '2026-11-19T00:00:01Z',
        })
        await asyncio.sleep(0)

        drained = []
        while not sub.queue.empty():
            drained.append(sub.queue.get_nowait())
        events = [item['event'] for item in drained]
        assert events.count('step.event') == 2
        assert [item['data']['event']['index'] for item in drained if item['event'] == 'step.event'] == [0, 1]
        assert drained[-1]['event'] == 'run.terminal'
        assert drained[-1]['id'] == 'done:completed'
        assert drained[-1]['data']['version_id'] == 'v2'

        # Extreme overflow of durable frames only -> flags resync and preserves terminal.
        sub2 = broker.subscribe('op-durable', account_id='acc-1', loop=loop)
        broker.publish_progress('op-durable', {
            'operation_status': 'running',
            'current': None,
            'usage': {'model_calls': 1, 'search_calls': 0, 'total_tokens': 0},
            'interim': {},
            'events': [{'index': i, 'kind': 'tool'} for i in range(6)],
            'event_count': 6,
            'updated_at': '2026-11-19T00:00:00Z',
        })
        broker.publish_terminal('op-durable', operation_status='completed', version_id='v3')
        await asyncio.sleep(0)
        assert sub2.needs_resync is True
        drained2 = []
        while not sub2.queue.empty():
            drained2.append(sub2.queue.get_nowait())
        assert drained2[-1]['event'] == 'run.terminal'

        # Grace window cleanup removes channels only after subscribers leave and grace elapses.
        broker.unsubscribe(sub)
        broker.unsubscribe(sub2)
        assert 'op-bp' in broker._channels
        now[0] += 29.0
        assert broker.prune_expired() == 0
        now[0] += 2.0
        assert broker.prune_expired() == 2
        assert broker._channels == {}

    asyncio.run(scenario())


def test_broker_l3_delta_and_reset_validation():
    async def scenario():
        broker = ResearchLiveBroker()
        loop = asyncio.get_running_loop()
        sub = broker.subscribe('op-l3', account_id='acc-1', loop=loop)
        for ch in ('thinking', 'text', 'tool_status', 'draft_report'):
            broker.publish_delta('op-l3', call_seq=1, channel=ch, delta=f'chunk-{ch}', seq=1)
        broker.publish_reset('op-l3', call_seq=1, reason='tool_start')
        with pytest.raises(ValueError):
            broker.publish_delta('op-l3', call_seq=1, channel='invalid', delta='x', seq=1)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        items = []
        while not sub.queue.empty():
            items.append(sub.queue.get_nowait())
        assert [i['event'] for i in items] == ['stream.delta'] * 4 + ['stream.reset']
        assert all(i['id'] is None for i in items)
        assert items[0]['data'] == {'operation_id': 'op-l3', 'call_seq': 1, 'channel': 'thinking', 'delta': 'chunk-thinking', 'seq': 1}
        assert items[3]['data']['channel'] == 'draft_report'
        assert items[4]['data'] == {'operation_id': 'op-l3', 'call_seq': 1, 'reason': 'tool_start'}
        broker.unsubscribe(sub)

    asyncio.run(scenario())


def test_sse_endpoint_auth_ownership_and_cursor_validation(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid)))
    operation_id = env.run()
    events_url = f'{env.path}/runs/{operation_id}/events'

    # Unauthenticated -> 401/403
    assert env.client.get(events_url).status_code in (401, 403)

    # Cross-account isolation -> 404
    other = TestClient(api.app)
    auth = other.post('/v1/auth/local/register', json={
        'email': f'{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!',
    }).json()
    other_headers = {'Authorization': 'Bearer ' + auth['access_token']}
    assert other.get(events_url, headers=other_headers).status_code == 404
    assert env.client.get(f'{env.path}/runs/op-missing/events', headers=env.headers).status_code == 404

    # Malformed Last-Event-ID -> 400 invalid_last_event_id
    bad = env.client.get(events_url, headers={**env.headers, 'Last-Event-ID': 'ev:not-an-int'})
    assert bad.status_code == 400
    assert bad.json()['error_code'] == 'invalid_last_event_id'

    # Negative after -> 422
    assert env.client.get(f'{events_url}?after=-1', headers=env.headers).status_code == 422

    # Already completed run immediately emits snapshot + terminal and closes cleanly.
    done_resp = env.client.get(events_url, headers=env.headers)
    assert done_resp.status_code == 200
    assert done_resp.headers['content-type'].startswith('text/event-stream')
    assert done_resp.headers['cache-control'] == 'no-cache, no-transform'
    assert done_resp.headers['connection'] == 'keep-alive'
    assert done_resp.headers['x-accel-buffering'] == 'no'
    frames = _parse_sse_frames(done_resp.text.splitlines())
    assert [f['event'] for f in frames] == ['progress.snapshot', 'run.terminal']
    assert frames[0]['id'] == 'cp:4'
    assert frames[0]['data']['operation_status'] == 'completed'
    assert frames[0]['data']['event_count'] == 4
    assert frames[1]['id'] == 'done:completed'
    assert frames[1]['data']['version_id'] == env.latest()['version_id']

    # Reconnecting with Last-Event-ID: done:completed also succeeds cleanly.
    done_reconnect = env.client.get(events_url, headers={**env.headers, 'Last-Event-ID': 'done:completed'})
    assert done_reconnect.status_code == 200
    assert [f['event'] for f in _parse_sse_frames(done_reconnect.text.splitlines())] == ['progress.snapshot', 'run.terminal']


def test_sse_endpoint_live_stream_resume_and_deltas(env, live_server):
    sid = env.source_id
    entered = threading.Event()
    release = threading.Event()

    def blocked(messages, kwargs):
        entered.set()
        assert release.wait(10)
        return submit(sid)(messages, kwargs)

    env.install(Scripted(read_all(sid), blocked))
    start_resp = env.start()
    assert start_resp.status_code == 202
    op_id = start_resp.json()['operation']['operation_id']
    assert entered.wait(10)

    async def scenario():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as client:
            # 1. Connect mid-run with Last-Event-ID: ev:0 (only event 1 should be in snapshot).
            async with client.stream(
                'GET',
                f'{env.path}/runs/{op_id}/events',
                headers={**env.headers, 'Last-Event-ID': 'ev:0'},
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers['content-type'].startswith('text/event-stream')
                assert resp.headers['cache-control'] == 'no-cache, no-transform'
                assert resp.headers['x-accel-buffering'] == 'no'

                lines_iter = resp.aiter_lines()
                # Read initial progress.snapshot frame
                snap_lines: list[str] = []
                async for line in lines_iter:
                    snap_lines.append(line)
                    if line == '':
                        break
                snap_frames = _parse_sse_frames(snap_lines)
                assert len(snap_frames) == 1
                assert snap_frames[0]['event'] == 'progress.snapshot'
                assert snap_frames[0]['id'] == 'cp:2'
                assert [e['index'] for e in snap_frames[0]['data']['events']] == [1]

                # Emit transient L3-ready delta + reset while live.
                api.research_live_broker.publish_delta(op_id, call_seq=2, channel='thinking', delta='analyzing', seq=1)
                api.research_live_broker.publish_reset(op_id, call_seq=2, reason='tool_start')

                # Release the blocked model call so remaining steps + terminal stream live.
                release.set()

                rest_lines: list[str] = []
                async for line in lines_iter:
                    rest_lines.append(line)
                rest_frames = _parse_sse_frames(rest_lines)

                event_names = [f['event'] for f in rest_frames]
                assert 'stream.delta' in event_names
                assert 'stream.reset' in event_names
                assert 'step.current' in event_names
                assert event_names[-1] == 'run.terminal'

                step_events = [f for f in rest_frames if f['event'] == 'step.event']
                assert [f['data']['event']['index'] for f in step_events] == [2, 3]
                assert [f['id'] for f in step_events] == ['ev:2', 'ev:3']
                # No duplicate event 0 or 1, no id on step.current / stream.delta / stream.reset.
                assert all(f['id'] is None for f in rest_frames if f['event'] in ('step.current', 'stream.delta', 'stream.reset'))
                term = rest_frames[-1]
                assert term['id'] == 'done:completed'
                assert term['data']['operation_id'] == op_id
                assert term['data']['operation_status'] == 'completed'
                assert term['data']['version_id'] is not None

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert env.wait(op_id)['status'] == 'completed'


def test_sse_endpoint_heartbeat_cancel_and_429_limit(env, live_server, monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_SSE_MAX_PER_ACCOUNT', '2')
    monkeypatch.setattr(api.research_live_broker, 'heartbeat_seconds', 0.05)

    sid = env.source_id
    entered = threading.Event()
    release = threading.Event()

    def blocked(messages, kwargs):
        entered.set()
        release.wait(10)
        return submit(sid)(messages, kwargs)

    env.install(Scripted(read_all(sid), blocked))
    start_resp = env.start()
    assert start_resp.status_code == 202
    op_json = start_resp.json()['operation']
    op_id = op_json['operation_id']
    cancel_token = op_json['cancel_token']
    assert entered.wait(10)

    other_client = TestClient(api.app)
    other_auth = other_client.post('/v1/auth/local/register', json={
        'email': f'{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!',
    }).json()
    other_headers = {'Authorization': 'Bearer ' + other_auth['access_token']}
    other_topic = other_client.post('/v2/research/topics', headers=other_headers, json={'question': Q1}).json()

    async def scenario():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as client:
            # Open 2 streams (max allowed per account)
            s1_cm = client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers)
            s2_cm = client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers)
            r1 = await s1_cm.__aenter__()
            r2 = await s2_cm.__aenter__()
            try:
                assert r1.status_code == 200
                assert r2.status_code == 200

                # 3rd stream for same account returns 429 BEFORE StreamingResponse
                r3 = await client.get(f'{env.path}/runs/{op_id}/events', headers=env.headers)
                assert r3.status_code == 429
                assert r3.json()['error_code'] == 'rate_limited'
                assert r3.headers['retry-after'] == '5'

                # Other account still has its own quota (and 404 on env's topic)
                r_other = await client.get(f"/v2/research/topics/{other_topic['topic_id']}/runs/{op_id}/events", headers=other_headers)
                assert r_other.status_code == 404

                # Close stream 2 -> allow heartbeat tick to detect TCP close and release slot -> new stream succeeds
                await s2_cm.__aexit__(None, None, None)
                await asyncio.sleep(0.15)
                s2_cm_new = client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers)
                r2_new = await s2_cm_new.__aenter__()
                try:
                    assert r2_new.status_code == 200
                finally:
                    await s2_cm_new.__aexit__(None, None, None)

                # Read from stream 1 until a heartbeat comment arrives, then cancel the operation.
                lines_iter = r1.aiter_lines()
                saw_heartbeat = False
                collected: list[str] = []
                for _ in range(40):
                    line = await asyncio.wait_for(lines_iter.__anext__(), timeout=2.0)
                    collected.append(line)
                    if line == ': heartbeat':
                        saw_heartbeat = True
                        break
                assert saw_heartbeat

                cancel_resp = await client.post(
                    f'/v1/operations/{op_id}/cancel',
                    headers={**env.headers, 'Idempotency-Key': uuid4().hex},
                    json={'cancel_token': cancel_token},
                )
                assert cancel_resp.status_code == 200
                assert cancel_resp.json()['operation']['status'] == 'cancelled'

                # Stream 1 receives run.terminal cancelled and closes cleanly.
                async for line in lines_iter:
                    collected.append(line)
                frames = _parse_sse_frames(collected)
                term = frames[-1]
                assert term['event'] == 'run.terminal'
                assert term['id'] == 'done:cancelled'
                assert term['data'] == {'operation_id': op_id, 'operation_status': 'cancelled', 'version_id': None}
            finally:
                await s1_cm.__aexit__(None, None, None)
                release.set()

    asyncio.run(scenario())


def test_sse_endpoint_failed_run_emits_terminal_failed(env, live_server):
    entered = threading.Event()
    release = threading.Event()

    class BoomProvider:
        name = 'boom'
        policy = type('P', (), {'timeout_seconds': 5, 'max_input_tokens': None, 'max_output_tokens': 1000})()

        def complete(self, messages, tools, **kwargs):
            entered.set()
            release.wait(10)
            raise RuntimeError('unexpected internal failure')

    env.install(BoomProvider())
    start_resp = env.start()
    assert start_resp.status_code == 202
    op_id = start_resp.json()['operation']['operation_id']
    assert entered.wait(10)

    async def scenario():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as client:
            async with client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers) as resp:
                assert resp.status_code == 200
                release.set()
                frames = await _read_sse_frames(resp)
                assert frames[0]['event'] == 'progress.snapshot'
                assert frames[-1]['event'] == 'run.terminal'
                assert frames[-1]['id'] == 'done:failed'
                assert frames[-1]['data'] == {'operation_id': op_id, 'operation_status': 'failed', 'version_id': None}

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert env.wait(op_id)['status'] == 'failed'


def test_sse_endpoint_heartbeat_catches_cross_process_storage_updates(env, live_server, monkeypatch):
    monkeypatch.setattr(api.research_live_broker, 'heartbeat_seconds', 0.05)
    sid = env.source_id
    entered = threading.Event()
    release = threading.Event()

    def blocked(messages, kwargs):
        entered.set()
        release.wait(10)
        return submit(sid)(messages, kwargs)

    env.install(Scripted(read_all(sid), blocked))
    op_id = env.start().json()['operation']['operation_id']
    assert entered.wait(10)

    async def scenario():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as client:
            async with client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers) as resp:
                assert resp.status_code == 200
                lines_iter = resp.aiter_lines()
                # Consume initial snapshot
                async for line in lines_iter:
                    if line == '':
                        break

                # Simulate another process appending a checkpoint event directly in storage without calling broker.
                stored = api.RESEARCH_RUN_INPUTS.get(op_id)
                stored['runtime_checkpoint']['events'].append({
                    'kind': 'tool',
                    'name': 'discover_sources',
                    'status': 'completed',
                    'rationale': 'cross-process event',
                })
                api.RESEARCH_RUN_INPUTS.put(op_id, stored)

                # Wait through heartbeat check -> should emit step.event for index 2!
                caught_lines: list[str] = []
                for _ in range(30):
                    line = await asyncio.wait_for(lines_iter.__anext__(), timeout=2.0)
                    caught_lines.append(line)
                    if line == '' and any('discover_sources' in item for item in caught_lines):
                        break
                frames = _parse_sse_frames(caught_lines)
                assert any(f.get('event') == 'step.event' and f['id'] == 'ev:2' for f in frames)

                # Simulate external cancellation committed in store without broker notification.
                op_doc = api.operations.store.get_operation(op_id)
                op_doc['status'] = 'cancelled'
                api.operations.store.put_operation(op_id, op_doc) if hasattr(api.operations.store, 'put_operation') else None
                # For InMemoryOperationStore, update stored operation directly via cancel if needed.
                if api.operations.store.get_operation(op_id)['status'] != 'cancelled':
                    api.operations.store._operations[op_id]['status'] = 'cancelled'

                term_lines: list[str] = []
                async for line in lines_iter:
                    term_lines.append(line)
                term_frames = _parse_sse_frames(term_lines)
                assert term_frames[-1]['event'] == 'run.terminal'
                assert term_frames[-1]['id'] == 'done:cancelled'

    try:
        asyncio.run(scenario())
    finally:
        release.set()
