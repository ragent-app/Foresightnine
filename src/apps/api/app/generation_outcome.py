"""Independent, serializable observations about one generation attempt.

A response only establishes transport state; it does not imply validation,
evidentiary support, or completion of the research task.
"""
from dataclasses import asdict, dataclass
from typing import Literal

Transport = Literal['not_sent', 'unknown', 'responded']
Structure = Literal['not_checked', 'valid', 'partial', 'invalid']
Evidence = Literal['not_checked', 'supported', 'partial', 'unsupported', 'contradicts', 'uncertain']
Completion = Literal['not_checked', 'complete', 'partial', 'failed', 'blocked', 'cancelled']

@dataclass(frozen=True)
class GenerationOutcome:
    transport: Transport = 'not_sent'
    structure: Structure = 'not_checked'
    evidence: Evidence = 'not_checked'
    completion: Completion = 'not_checked'

    def __post_init__(self) -> None:
        allowed = {
            'transport': {'not_sent', 'unknown', 'responded'},
            'structure': {'not_checked', 'valid', 'partial', 'invalid'},
            'evidence': {'not_checked', 'supported', 'partial', 'unsupported', 'contradicts', 'uncertain'},
            'completion': {'not_checked', 'complete', 'partial', 'failed', 'blocked', 'cancelled'},
        }
        for name, values in allowed.items():
            if getattr(self, name) not in values:
                raise ValueError(f'invalid {name} state')

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def transport_from_submission(state: object) -> Transport:
    if state == 'not_started':
        return 'not_sent'
    if state == 'response_received':
        return 'responded'
    return 'unknown'
