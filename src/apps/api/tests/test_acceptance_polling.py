"""Acceptance recording uses local fakes only; it never invokes a model."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'docs/product/phase-a/336-stage-b-acceptance-budget-fix/live_acceptance.py'
spec = importlib.util.spec_from_file_location('acceptance_polling', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body
    def json(self):
        return self.body


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
    def get(self, *_args, **_kwargs):
        return next(self.responses)


def test_poll_retains_safe_errors_and_recovers_terminal_status():
    events = []
    client = Client([Response(429, {'error_code': 'rate_limited', 'message': 'private details'}),
                     Response(200, {'operation': {'status': 'running'}}),
                     Response(200, {'operation': {'status': 'completed'}})])
    state = module.poll_operation(client, '/topic', {}, 'operation', deadline=10,
                                  record=events.append, now=lambda: 0, sleep=lambda _: None)
    assert state['status'] == 'completed'
    assert [e['http_status'] for e in events] == [429, 200, 200]
    assert events[0]['error_code'] == 'rate_limited'
    assert 'private details' not in repr(events)


def test_poll_deadline_records_unknown_without_inventing_completion():
    events = []
    state = module.poll_operation(Client([]), '/topic', {}, 'operation', deadline=1,
                                  record=events.append, now=lambda: 2, sleep=lambda _: None)
    assert state is None
    assert events[-1]['operation_status'] == 'unknown'
