"""Explicit synthetic reviewer receipt for transport / lifecycle tests, never a quality score."""
import json

from app.model_protocol import ModelResponse, ModelUsage


def fixture_review(user_payload: str, verdict='supported') -> ModelResponse:
    payload = json.loads(user_payload)
    result = []
    for candidate in payload['candidates']:
        result.append({**{k: candidate[k] for k in ('requirement_id', 'claim_id', 'review_key')},
                       'evidence_ids': [item['evidence_id'] for item in candidate['evidence_context']],
                       'verdict': verdict, 'reason': 'Fixed verdict for this workflow fixture.',
                       'condition': None})
    return ModelResponse({'role': 'assistant', 'content': json.dumps({'reviews': result})},
                         (), ModelUsage(4, 3, 7), 'stop')
