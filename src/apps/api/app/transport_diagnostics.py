"""Per-call bounded transport observations; never retain arbitrary network data."""
from contextlib import contextmanager
from threading import Lock
from time import monotonic

class TransportDiagnostics:
    def __init__(self):
        self._lock = Lock()
        self._stages = []
        self._submission = 'not_started'
        self._frozen = False
        self._active = None

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

    def interrupt(self, reason, elapsed_ms):
        """Freeze the observed active stage while its worker may still be blocked."""
        result = 'cancelled' if reason == 'cancelled' else 'timeout'
        with self._lock:
            if self._frozen:
                return
            # Reserve room for the active phase and the terminal reason even
            # after many address attempts; retain the most recent history.
            self._stages = self._stages[-62:]
            if self._active is not None:
                name, start = self._active
                self._stages.append({'stage': name, 'elapsed_ms': max(0, int((monotonic()-start)*1000)), 'result': result})
            self._stages.append({'stage': reason, 'elapsed_ms': max(0, int(elapsed_ms)), 'result': result})
            self._active = None
            self._frozen = True

    def snapshot(self):
        with self._lock:
            self._frozen = True
            return {'submission_state': self._submission, 'stages': [dict(x) for x in self._stages]}
