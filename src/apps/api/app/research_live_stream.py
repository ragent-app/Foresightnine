"""In-process thread-safe live SSE broker for v2 research runs.

Streaming frames are transient read-only views backed by durable checkpoints in
PostgreSQL/storage. Publishing from the research worker thread never blocks on
slow SSE consumers; full subscriber queues drop transient delta/current frames
first so durable step events and terminal notifications are preserved.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from threading import Lock, RLock, Timer
from time import monotonic
from typing import Any, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from .operations import ApiProblem

StreamChannel = Literal['thinking', 'text', 'tool_status', 'draft_report']
_STREAM_CHANNELS = frozenset({'thinking', 'text', 'tool_status', 'draft_report'})
ORG_GRAPH_ACTION_KINDS = frozenset({
    'READING',
    'SEARCHING',
    'INSPECTING',
    'DELEGATING',
    'WAITING_SUBORDINATES',
    'RESPONDING',
    'NEEDS_USER',
    'BLOCKED',
    'FAILED',
})
_TERMINAL_STATUSES = frozenset({'completed', 'failed', 'cancelled'})
_DEFAULT_MAX_PER_ACCOUNT = 6
_DEFAULT_QUEUE_MAXSIZE = 128
_DEFAULT_GRACE_SECONDS = 30.0
_DEFAULT_HEARTBEAT_SECONDS = 15.0


def max_sse_streams_per_account() -> int:
    raw = os.environ.get('FORESIGHTNINE_RESEARCH_SSE_MAX_PER_ACCOUNT', str(_DEFAULT_MAX_PER_ACCOUNT)).strip()
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_PER_ACCOUNT
    return value if 1 <= value <= 100 else _DEFAULT_MAX_PER_ACCOUNT


def parse_last_event_id(last_event_id: str | None) -> tuple[int, bool]:
    """Parse SSE Last-Event-ID into `(after_index, is_terminal_marker)` or raise 400."""
    if last_event_id is None or last_event_id == '':
        return 0, False
    if not last_event_id.strip():
        raise ApiProblem(400, 'invalid_last_event_id', 'Last-Event-ID is invalid.')
    if last_event_id.startswith('ev:'):
        raw = last_event_id[3:]
        if not (raw.isascii() and raw.isdigit()):
            raise ApiProblem(400, 'invalid_last_event_id', 'Last-Event-ID event index is invalid.')
        return int(raw) + 1, False
    if last_event_id.startswith('cp:'):
        raw = last_event_id[3:]
        if not (raw.isascii() and raw.isdigit()):
            raise ApiProblem(400, 'invalid_last_event_id', 'Last-Event-ID snapshot cursor is invalid.')
        return int(raw), False
    if last_event_id.startswith('done:'):
        status = last_event_id[5:]
        if status not in _TERMINAL_STATUSES:
            raise ApiProblem(400, 'invalid_last_event_id', 'Last-Event-ID terminal status is invalid.')
        return 0, True
    raise ApiProblem(400, 'invalid_last_event_id', 'Last-Event-ID format is invalid.')


def format_sse_frame(event: str, data: Mapping[str, Any], *, event_id: str | None = None) -> str:
    """Serialize one SSE frame with optional id and compact JSON data."""
    lines = [f'event: {event}']
    if event_id is not None:
        lines.append(f'id: {event_id}')
    lines.append('data: ' + json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str))
    return '\n'.join(lines) + '\n\n'


def parse_org_last_event_id(last_event_id: str | None) -> int:
    """Parse SSE Last-Event-ID cursor for org_graph_event streams (`org:<seq>` -> `<seq>`)."""
    if last_event_id is None or last_event_id == '':
        return 0
    if not last_event_id.strip():
        raise ApiProblem(400, 'invalid_last_event_id', 'Last-Event-ID is invalid.')
    if last_event_id.startswith('org:'):
        raw = last_event_id[4:]
        if not (raw.isascii() and raw.isdigit()):
            raise ApiProblem(400, 'invalid_last_event_id', 'Last-Event-ID org sequence is invalid.')
        return int(raw)
    after_idx, _ = parse_last_event_id(last_event_id)
    return after_idx


def _coerce_org_edge_event(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if hasattr(raw, 'model_dump') and callable(raw.model_dump):
        raw = raw.model_dump(mode='json')
    if not isinstance(raw, Mapping):
        raise ValueError('edge_event must be a mapping or None.')
    from_id = str(raw.get('from_node_id') or '').strip()
    to_id = str(raw.get('to_node_id') or '').strip()
    if not from_id or not to_id:
        raise ValueError('edge_event requires non-blank from_node_id and to_node_id.')
    out: dict[str, Any] = {
        'from_node_id': from_id,
        'to_node_id': to_id,
        'is_review': bool(raw.get('is_review', False)),
    }
    if raw.get('edge_kind') is not None:
        out['edge_kind'] = str(raw['edge_kind'])
    if raw.get('label') is not None:
        out['label'] = str(raw['label'])
    return out


def normalize_org_graph_event(event: Any) -> dict[str, Any]:
    """Validate and normalize an OrgGraphEvent into a strict JSON-serializable payload."""
    if hasattr(event, 'model_dump') and callable(event.model_dump):
        raw = event.model_dump(mode='json')
    elif isinstance(event, Mapping):
        raw = dict(event)
    else:
        raise ValueError('OrgGraphEvent payload must be a model or mapping.')

    seq = raw.get('sequence')
    if type(seq) is not int or seq < 1:
        raise ValueError('OrgGraphEvent.sequence must be an integer >= 1.')

    required_fields: dict[str, str] = {}
    for fname in ('stage', 'node_id', 'role', 'display_name', 'runtime_state', 'action_kind', 'current_activity'):
        val = raw.get(fname)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f'OrgGraphEvent.{fname} must be a non-blank string.')
        required_fields[fname] = val.strip()

    if required_fields['action_kind'] not in ORG_GRAPH_ACTION_KINDS:
        raise ValueError(f"Unsupported OrgGraphEvent.action_kind: {required_fields['action_kind']!r}")

    blocker = raw.get('blocker_text')
    blocker_text = str(blocker) if blocker is not None else None
    edge_event = _coerce_org_edge_event(raw.get('edge_event'))

    normalized: dict[str, Any] = {
        'sequence': seq,
        **required_fields,
        'blocker_text': blocker_text,
        'edge_event': edge_event,
    }
    for optional_key in ('timestamp', 'slot', 'parent_node_id'):
        if raw.get(optional_key) is not None:
            normalized[optional_key] = raw[optional_key]
    return normalized


def format_org_graph_sse_frame(operation_id: str, event: Any) -> str:
    """Format one ``org_graph_event`` SSE frame with ``id: org:<sequence>``."""
    payload = normalize_org_graph_event(event)
    return format_sse_frame(
        'org_graph_event',
        {'operation_id': operation_id, **payload},
        event_id=f"org:{payload['sequence']}",
    )


@dataclass
class _Subscriber:
    subscriber_id: str
    account_id: str
    operation_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]
    closed: bool = False
    needs_resync: bool = False


@dataclass
class _OperationChannel:
    operation_id: str
    subscribers: dict[str, _Subscriber] = field(default_factory=dict)
    last_event_index: int = -1
    last_org_sequence: int = 0
    last_current_key: str | None = None
    terminal_at: float | None = None


def _deliver_on_loop(sub: _Subscriber, frame: dict[str, Any]) -> None:
    """Deliver one frame on the subscriber's own event loop thread without blocking."""
    if sub.closed:
        return
    if not sub.queue.full():
        sub.queue.put_nowait(frame)
        return
    event_name = frame.get('event')
    if event_name == 'stream.delta':
        return
    items: list[dict[str, Any]] = []
    while not sub.queue.empty():
        items.append(sub.queue.get_nowait())
    evict_index: int | None = None
    for target in ('stream.delta', 'step.current', 'stream.reset'):
        evict_index = next((idx for idx, item in enumerate(items) if item.get('event') == target), None)
        if evict_index is not None:
            break
    if evict_index is not None:
        items.pop(evict_index)
        for item in items:
            sub.queue.put_nowait(item)
        sub.queue.put_nowait(frame)
        return
    if event_name in ('step.current', 'stream.reset'):
        for item in items:
            sub.queue.put_nowait(item)
        if event_name == 'step.current':
            sub.needs_resync = True
        return
    # Queue holds only durable frames; drop the oldest and request snapshot resync.
    if items:
        items.pop(0)
        sub.needs_resync = True
    for item in items:
        sub.queue.put_nowait(item)
    sub.queue.put_nowait(frame)


class ResearchLiveBroker:
    """Thread-safe in-process SSE pub/sub broker keyed by operation_id."""

    def __init__(
        self,
        *,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        grace_seconds: float = _DEFAULT_GRACE_SECONDS,
        heartbeat_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.queue_maxsize = max(1, queue_maxsize)
        self.grace_seconds = max(0.0, grace_seconds)
        self.heartbeat_seconds = max(0.01, heartbeat_seconds)
        self._clock = clock or monotonic
        self._lock = RLock()
        self._channels: dict[str, _OperationChannel] = {}
        self._account_subscribers: dict[str, set[str]] = {}

    def reset(self) -> None:
        with self._lock:
            for channel in self._channels.values():
                for sub in channel.subscribers.values():
                    sub.closed = True
            self._channels.clear()
            self._account_subscribers.clear()

    def prune_expired(self) -> int:
        with self._lock:
            return self._prune_expired_locked()

    def _prune_expired_locked(self) -> int:
        now = self._clock()
        expired = [
            op_id
            for op_id, channel in self._channels.items()
            if channel.terminal_at is not None
            and not channel.subscribers
            and now - channel.terminal_at >= self.grace_seconds
        ]
        for op_id in expired:
            del self._channels[op_id]
        return len(expired)

    def active_streams_for_account(self, account_id: str) -> int:
        with self._lock:
            return len(self._account_subscribers.get(account_id, ()))

    def subscribe(self, operation_id: str, *, account_id: str, loop: asyncio.AbstractEventLoop) -> _Subscriber:
        with self._lock:
            self._prune_expired_locked()
            active = self._account_subscribers.get(account_id)
            if active is not None and len(active) >= max_sse_streams_per_account():
                raise ApiProblem(
                    429,
                    'rate_limited',
                    'Too many concurrent research event streams.',
                    retryable=True,
                    retry_after=5,
                )
            channel = self._channels.setdefault(operation_id, _OperationChannel(operation_id))
            sub = _Subscriber(
                subscriber_id=uuid4().hex,
                account_id=account_id,
                operation_id=operation_id,
                loop=loop,
                queue=asyncio.Queue(maxsize=self.queue_maxsize),
            )
            channel.subscribers[sub.subscriber_id] = sub
            self._account_subscribers.setdefault(account_id, set()).add(sub.subscriber_id)
            return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            sub.closed = True
            channel = self._channels.get(sub.operation_id)
            if channel is not None:
                channel.subscribers.pop(sub.subscriber_id, None)
            account_set = self._account_subscribers.get(sub.account_id)
            if account_set is not None:
                account_set.discard(sub.subscriber_id)
                if not account_set:
                    del self._account_subscribers[sub.account_id]
            self._prune_expired_locked()

    @staticmethod
    def _dispatch(subs: list[_Subscriber], frames: list[dict[str, Any]]) -> None:
        for sub in subs:
            if sub.closed or sub.loop.is_closed():
                continue
            for frame in frames:
                try:
                    sub.loop.call_soon_threadsafe(_deliver_on_loop, sub, frame)
                except RuntimeError:
                    sub.closed = True
                    break

    @staticmethod
    def _collect_new_event_frames(
        channel: _OperationChannel, operation_id: str, progress: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        events = progress.get('events') if isinstance(progress.get('events'), list) else []
        for ev in events:
            if not isinstance(ev, Mapping):
                continue
            idx = ev.get('index')
            if type(idx) is not int or idx <= channel.last_event_index:
                continue
            channel.last_event_index = idx
            frames.append({
                'event': 'step.event',
                'id': f'ev:{idx}',
                'data': {
                    'operation_id': operation_id,
                    'event': dict(ev),
                    'event_count': progress.get('event_count', idx + 1),
                    'usage': progress.get('usage'),
                    'interim': progress.get('interim'),
                    'updated_at': progress.get('updated_at'),
                },
            })
        return frames

    @staticmethod
    def _collect_new_org_graph_frames(
        channel: _OperationChannel, operation_id: str, progress: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        raw_events = progress.get('org_events')
        if not isinstance(raw_events, list) or not raw_events:
            return []
        frames: list[dict[str, Any]] = []
        for raw_ev in raw_events:
            payload = normalize_org_graph_event(raw_ev)
            seq = payload['sequence']
            if seq <= channel.last_org_sequence:
                continue
            channel.last_org_sequence = seq
            frames.append({
                'event': 'org_graph_event',
                'id': f'org:{seq}',
                'data': {
                    'operation_id': operation_id,
                    **payload,
                },
            })
        return frames

    def publish_org_graph_event(self, operation_id: str, event: Any) -> dict[str, Any] | None:
        """Emit a single strictly-sequenced ``org_graph_event`` SSE frame to active subscribers."""
        payload = normalize_org_graph_event(event)
        seq = payload['sequence']
        with self._lock:
            self._prune_expired_locked()
            channel = self._channels.setdefault(operation_id, _OperationChannel(operation_id))
            if channel.terminal_at is not None or seq <= channel.last_org_sequence:
                return None
            channel.last_org_sequence = seq
            frame = {
                'event': 'org_graph_event',
                'id': f'org:{seq}',
                'data': {
                    'operation_id': operation_id,
                    **payload,
                },
            }
            subs = list(channel.subscribers.values())
        if subs:
            self._dispatch(subs, [frame])
        return frame

    def publish_org_graph_batch(self, operation_id: str, events: Sequence[Any]) -> list[dict[str, Any]]:
        """Emit an ordered batch of ``org_graph_event`` frames while deduplicating sequences."""
        normalized = [normalize_org_graph_event(ev) for ev in events]
        normalized.sort(key=lambda item: item['sequence'])
        frames: list[dict[str, Any]] = []
        with self._lock:
            self._prune_expired_locked()
            channel = self._channels.setdefault(operation_id, _OperationChannel(operation_id))
            if channel.terminal_at is not None:
                return []
            for payload in normalized:
                seq = payload['sequence']
                if seq <= channel.last_org_sequence:
                    continue
                channel.last_org_sequence = seq
                frames.append({
                    'event': 'org_graph_event',
                    'id': f'org:{seq}',
                    'data': {
                        'operation_id': operation_id,
                        **payload,
                    },
                })
            subs = list(channel.subscribers.values())
        if frames and subs:
            self._dispatch(subs, frames)
        return frames

    def publish_progress(self, operation_id: str, progress: dict[str, Any]) -> None:
        with self._lock:
            self._prune_expired_locked()
            channel = self._channels.setdefault(operation_id, _OperationChannel(operation_id))
            if channel.terminal_at is not None:
                return
            frames = self._collect_new_event_frames(channel, operation_id, progress)
            frames.extend(self._collect_new_org_graph_frames(channel, operation_id, progress))
            current_state = {
                'operation_status': progress.get('operation_status'),
                'current': progress.get('current'),
                **{key: progress[key] for key in ('logical_status', 'instructions', 'current_operation_id')
                   if key in progress},
                'usage': progress.get('usage'),
                'interim': progress.get('interim'),
            }
            current_key = json.dumps(current_state, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            if current_key != channel.last_current_key:
                channel.last_current_key = current_key
                frames.append({
                    'event': 'step.current',
                    'id': None,
                    'data': {
                        'operation_id': operation_id,
                        **current_state,
                        'updated_at': progress.get('updated_at'),
                    },
                })
            subs = list(channel.subscribers.values())
        if frames and subs:
            self._dispatch(subs, frames)

    def publish_stage_event(
        self,
        operation_id: str,
        *,
        event_name: str,
        payload: dict[str, Any],
        event_id: str | None = None,
    ) -> None:
        """Directly dispatch a structured stage SSE frame (e.g., V3a layer0_ready -> layer2_extracting)."""
        with self._lock:
            self._prune_expired_locked()
            channel = self._channels.setdefault(operation_id, _OperationChannel(operation_id))
            if channel.terminal_at is not None:
                return
            subs = list(channel.subscribers.values())
        if subs:
            self._dispatch(
                subs,
                [
                    {
                        'event': event_name,
                        'id': event_id,
                        'data': {
                            'operation_id': operation_id,
                            **payload,
                        },
                    }
                ],
            )

    def publish_delta(
        self,
        operation_id: str,
        *,
        call_seq: int,
        channel: StreamChannel,
        delta: str,
        seq: int,
    ) -> None:
        if channel not in _STREAM_CHANNELS:
            raise ValueError(f'Unsupported stream channel: {channel}')
        with self._lock:
            op_channel = self._channels.get(operation_id)
            if op_channel is None or op_channel.terminal_at is not None:
                return
            subs = list(op_channel.subscribers.values())
        if subs:
            self._dispatch(subs, [{
                'event': 'stream.delta',
                'id': None,
                'data': {
                    'operation_id': operation_id,
                    'call_seq': call_seq,
                    'channel': channel,
                    'delta': delta,
                    'seq': seq,
                },
            }])

    def publish_reset(self, operation_id: str, *, call_seq: int, reason: str) -> None:
        with self._lock:
            op_channel = self._channels.get(operation_id)
            if op_channel is None or op_channel.terminal_at is not None:
                return
            subs = list(op_channel.subscribers.values())
        if subs:
            self._dispatch(subs, [{
                'event': 'stream.reset',
                'id': None,
                'data': {
                    'operation_id': operation_id,
                    'call_seq': call_seq,
                    'reason': reason,
                },
            }])

    def publish_terminal(
        self,
        operation_id: str,
        *,
        operation_status: str,
        version_id: str | None = None,
        progress: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            channel = self._channels.setdefault(operation_id, _OperationChannel(operation_id))
            if channel.terminal_at is not None:
                return
            frames = self._collect_new_event_frames(channel, operation_id, progress) if progress is not None else []
            if progress is not None:
                frames.extend(self._collect_new_org_graph_frames(channel, operation_id, progress))
            frames.append({
                'event': 'run.terminal',
                'id': f'done:{operation_status}',
                'data': {
                    'operation_id': operation_id,
                    'operation_status': operation_status,
                    'status': operation_status,
                    'version_id': version_id,
                    **{key: progress[key] for key in (
                        'logical_status', 'current_operation_id', 'final_sequence',
                        'stage', 'entity_key', 'overall_light', 'error',
                    )
                       if progress and key in progress},
                },
            })
            channel.terminal_at = self._clock()
            subs = list(channel.subscribers.values())
            self._prune_expired_locked()
        if subs:
            self._dispatch(subs, frames)


class DeltaCoalescer:
    """In-memory delta batcher (~60ms or >=64 chars) scoped to one model attempt."""

    def __init__(
        self,
        *,
        call_seq: int,
        sink: Callable[[dict[str, Any]], None],
        flush_interval_seconds: float = 0.06,
        flush_chars: int = 64,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.call_seq = call_seq
        self._sink = sink
        self._flush_interval = max(0.001, flush_interval_seconds)
        self._flush_chars = max(1, flush_chars)
        self._clock = clock or monotonic
        self._lock = Lock()
        self._channel: StreamChannel | None = None
        self._buf: list[str] = []
        self._buf_len: int = 0
        self._buf_start: float | None = None
        self._timer: Timer | None = None
        self._timer_gen: int = 0
        self._seq: int = 0
        self._closed: bool = False

    def _cancel_timer_locked(self) -> None:
        self._timer_gen += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _flush_locked(self) -> None:
        self._cancel_timer_locked()
        if not self._buf or self._channel is None:
            self._buf_start = None
            return
        text = ''.join(self._buf)
        channel = self._channel
        self._buf.clear()
        self._buf_len = 0
        self._channel = None
        self._buf_start = None
        self._seq += 1
        self._sink({
            'type': 'delta',
            'call_seq': self.call_seq,
            'channel': channel,
            'delta': text,
            'seq': self._seq,
        })

    def _on_timer(self, gen: int) -> None:
        with self._lock:
            if gen != self._timer_gen:
                return
            self._timer = None
            if not self._closed:
                self._flush_locked()

    def _start_timer_locked(self) -> None:
        if self._closed or self._timer is not None:
            return
        gen = self._timer_gen
        timer = Timer(self._flush_interval, self._on_timer, args=(gen,))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def push_event(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            return
        channel = event.get('channel')
        delta = event.get('delta')
        if channel not in _STREAM_CHANNELS or not isinstance(delta, str) or not delta:
            return
        with self._lock:
            if self._closed:
                return
            if self._channel is not None and self._channel != channel:
                self._flush_locked()
            self._channel = channel
            self._buf.append(delta)
            self._buf_len += len(delta)
            now = self._clock()
            if (
                channel == 'tool_status'
                or self._buf_len >= self._flush_chars
                or (self._buf_start is not None and now - self._buf_start >= self._flush_interval)
            ):
                self._flush_locked()
            elif self._timer is None:
                self._buf_start = now
                self._start_timer_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        with self._lock:
            self._flush_locked()
            self._closed = True
