"""Per-call bounded transport observations; never retain arbitrary network data."""
from contextlib import contextmanager
from threading import Lock
from time import monotonic

VALID_TIMEOUT_KINDS = frozenset({
    'connect_timeout',
    'header_timeout',
    'stream_idle_timeout',
    'stream_deadline_timeout',
    'response_timeout',
})


class TransportDiagnostics:
    def __init__(self):
        self._lock = Lock()
        self._stages = []
        self._submission = 'not_started'
        self._frozen = False
        self._active = None
        self._timeout_kind = None

    @property
    def timeout_kind(self):
        with self._lock:
            return self._timeout_kind

    def set_timeout_kind(self, value):
        if isinstance(value, str) and value in VALID_TIMEOUT_KINDS:
            with self._lock:
                if not self._frozen:
                    self._timeout_kind = value

    def _infer_timeout_kind_locked(self, *, streaming=False, deadline_expired=True, got_headers=False):
        active_name = self._active[0] if self._active is not None else None
        if active_name in ('dns', 'tcp', 'tls', 'request_write'):
            return 'connect_timeout'
        if active_name == 'response_headers':
            return 'header_timeout'
        if active_name == 'response_body':
            if deadline_expired:
                return 'stream_deadline_timeout'
            return 'stream_idle_timeout' if streaming else 'response_timeout'
        if got_headers or self._submission == 'response_received' or any(
            item.get('stage') == 'response_headers' and item.get('result') == 'ok'
            for item in self._stages
        ):
            if deadline_expired:
                return 'stream_deadline_timeout'
            return 'stream_idle_timeout' if streaming else 'response_timeout'
        if any(
            item.get('stage') == 'request_write' and item.get('result') == 'ok'
            for item in self._stages
        ):
            return 'header_timeout'
        return 'connect_timeout'

    def submission(self, value):
        with self._lock:
            if not self._frozen:
                self._submission = value

    def record(self, stage, elapsed_ms, result='ok', *, error=None, status_code=None):
        item = {'stage': stage, 'elapsed_ms': max(0, int(elapsed_ms)), 'result': result}
        number = getattr(error, 'errno', None)
        if type(number) is int and 0 <= number <= 65535:
            item['errno'] = number
        if type(status_code) is int and 100 <= status_code <= 599:
            item['status_code'] = status_code
        with self._lock:
            if not self._frozen and len(self._stages) < 64:
                self._stages.append(item)

    @contextmanager
    def stage(self, name):
        start = monotonic()
        token = (name, start)
        with self._lock:
            if not self._frozen:
                self._active = token
        try:
            yield
        except BaseException as error:
            result = 'timeout' if isinstance(error, TimeoutError) else 'error'
            self._finish_stage(token, result, error)
            raise
        else:
            self._finish_stage(token, 'ok', None)

    def _finish_stage(self, token, result, error):
        name, start = token
        item = {'stage': name, 'elapsed_ms': max(0, int((monotonic()-start)*1000)), 'result': result}
        number = getattr(error, 'errno', None)
        if type(number) is int and 0 <= number <= 65535:
            item['errno'] = number
        with self._lock:
            if not self._frozen:
                if self._active == token:
                    self._active = None
                if len(self._stages) < 64:
                    self._stages.append(item)

    def _freeze_interrupt_locked(self, reason, elapsed_ms, result):
        """Append active and terminal interrupt stages under lock and freeze diagnostics."""
        # Reserve room for the active phase and the terminal reason even
        # after many address attempts; retain the most recent history.
        self._stages = self._stages[-62:]
        if self._active is not None:
            name, start = self._active
            self._stages.append({'stage': name, 'elapsed_ms': max(0, int((monotonic()-start)*1000)), 'result': result})
        self._stages.append({'stage': reason, 'elapsed_ms': max(0, int(elapsed_ms)), 'result': result})
        self._active = None
        self._frozen = True

    def interrupt(self, reason, elapsed_ms, *, timeout_kind=None, infer_timeout=False, streaming=False, deadline_expired=True):
        """Freeze the observed active stage while its worker may still be blocked."""
        result = 'cancelled' if reason == 'cancelled' else 'timeout'
        with self._lock:
            if self._frozen:
                return self._timeout_kind
            if result == 'cancelled':
                self._timeout_kind = None
            elif isinstance(timeout_kind, str) and timeout_kind in VALID_TIMEOUT_KINDS:
                self._timeout_kind = timeout_kind
            elif infer_timeout:
                self._timeout_kind = self._infer_timeout_kind_locked(streaming=streaming, deadline_expired=deadline_expired)
            self._freeze_interrupt_locked(reason, elapsed_ms, result)
            return self._timeout_kind

    def interrupt_stream_timeout(self, elapsed_ms, *, deadline_expired, got_headers=False):
        """Atomically classify and freeze a streaming timeout on the active stage."""
        with self._lock:
            if self._frozen:
                return self._timeout_kind
            kind = self._infer_timeout_kind_locked(
                streaming=True,
                deadline_expired=deadline_expired,
                got_headers=got_headers,
            )
            self._timeout_kind = kind
            if deadline_expired:
                reason = 'deadline'
            elif kind == 'stream_idle_timeout':
                reason = 'response_body'
            elif kind == 'header_timeout':
                reason = 'response_headers'
            else:
                reason = 'deadline'
            self._freeze_interrupt_locked(reason, elapsed_ms, 'timeout')
            return self._timeout_kind

    def snapshot(self):
        with self._lock:
            self._frozen = True
            snap = {'submission_state': self._submission, 'stages': [dict(x) for x in self._stages]}
            if self._timeout_kind is not None:
                snap['timeout_kind'] = self._timeout_kind
            return snap
