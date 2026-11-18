"""Isolated in-memory API for local browser acceptance; never a real model."""
from types import SimpleNamespace
import uvicorn
import app.main as api

class MockResearchProvider:
    name = 'local_ui_acceptance_mock'
    policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=8000, max_output_tokens=1000)
    def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
        return {'claims': [{'text': 'Synthetic finding for UI acceptance.', 'citations': []}],
                'counterarguments': [], 'limitations': ['Synthetic provider result; not financial research.']}, 100

api.challenge_provider = MockResearchProvider()
if __name__ == '__main__':
    uvicorn.run(api.app, host='127.0.0.1', port=8769, log_level='warning')
