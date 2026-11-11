from datetime import datetime, timezone
from types import SimpleNamespace

import app.main as main
from app.phase3 import ChallengeInput, DeterministicMockLlmProvider
from app.providers import FixedClock


class AuditClock:
    def now(self):
        return datetime(2026, 10, 3, 1, 2, 3, tzinfo=timezone.utc)


def test_live_metadata_uses_runtime_not_fixture_clock(monkeypatch):
    clock = AuditClock()
    monkeypatch.setattr(main, 'lifecycle_clock', clock)
    metadata = main._metadata({'sample': 'nonfactual'}, 'test:sample', 'Synthetic test')
    assert metadata['created_at'] == clock.now()
    assert main.clock.now() == FixedClock().now()  # mock datasets retain their historical clock


def test_search_retrieval_records_actual_clock_without_changing_publication(monkeypatch):
    clock = AuditClock()
    monkeypatch.setattr(main, 'lifecycle_clock', clock)
    published = FixedClock().today()
    candidate = main._candidate_response(SimpleNamespace(
        title='Synthetic source', url='https://example.com/evidence', excerpt='Synthetic excerpt',
        publisher='Test', published_at=published, independently_reviewable=False,
    ), 'test-provider')
    assert candidate.retrieved_at == clock.now()
    assert candidate.published_at.date() == published


def test_confirmation_records_confirmation_time_not_fixture_date(monkeypatch):
    clock = AuditClock()
    monkeypatch.setattr(main, 'lifecycle_clock', clock)
    request = ChallengeInput(operation_id='test-op', challenge_id='test-challenge', thesis_id='test-thesis',
        claim='Synthetic research premise.', turn=1, status='CHALLENGING', private_evidence_declared=False)
    current = main._challenge_from_draft(DeterministicMockLlmProvider().generate_challenge(request),
        operation_id='test-op', challenge_id='test-challenge', thesis_id='test-thesis', turn=1,
        status='CHALLENGING', private_evidence_declared=False)
    confirmed = main._confirmed_challenge(current, operation_id='test-confirm', turn=2,
        confirmation='confirmed_with_reservations', reservation_note='Synthetic unverified premise.')
    assert confirmed.created_at == clock.now()
    assert confirmed.status == 'CONFIRMED'
