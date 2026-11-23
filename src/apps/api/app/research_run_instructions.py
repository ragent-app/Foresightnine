"""Durable, per-turn user instructions and execution fencing.

The repository must support cross-worker advisory locks. All state for a turn
is coordinated by a small metadata document, so instruction receipt, stop,
and sealing serialize together. Individual messages and actions are separate.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4


class InstructionNotFound(Exception):
    pass


class InstructionConflict(Exception):
    pass


class InstructionInvalid(ValueError):
    pass


class ResearchRunInstructions:
    def __init__(self, repository):
        if not callable(getattr(repository, 'with_advisory_lock', None)):
            raise TypeError('Repository must provide with_advisory_lock')
        self.repository = repository

    def _locked(self, turn_id, fn):
        # Domain errors are returned through the callback: PostgreSQL's lock
        # wrapper translates raised exceptions into storage errors.
        def run():
            try:
                return (True, fn())
            except (InstructionNotFound, InstructionConflict, InstructionInvalid) as exc:
                return (False, exc)
        ok, value = self.repository.with_advisory_lock('research-turn:' + turn_id, run)
        if not ok:
            raise value
        return value

    def _get(self, account_id, topic_id, turn_id):
        doc = self.repository.get(turn_id)
        if not isinstance(doc, dict) or doc.get('account_id') != account_id or doc.get('topic_id') != topic_id:
            raise InstructionNotFound()
        return deepcopy(doc)

    def begin(self, account_id, topic_id, turn_id, operation_id, budget=None, *, previous_operation_id=None, allow_reopen=False, require_waiting=False):
        def run():
            existing = self.repository.get(turn_id)
            if existing is not None:
                doc = self._get(account_id, topic_id, turn_id)
                if doc['operation_id'] == operation_id:
                    return doc
                if require_waiting and doc['status'] != 'awaiting_user':
                    raise InstructionConflict('clarification is no longer waiting')
                if previous_operation_id != doc['operation_id']:
                    raise InstructionConflict('operation changed')
                if doc['status'] != 'open':
                    if not allow_reopen or doc['status'] not in ('stopped', 'sealed', 'awaiting_user'):
                        raise InstructionConflict('turn closed')
                    doc['status'] = 'open'
                    doc['final_version_id'] = None
                    doc.pop('clarification', None)
                doc['operation_id'] = operation_id
            else:
                if previous_operation_id is not None:
                    raise InstructionNotFound()
                doc = {'account_id': account_id, 'topic_id': topic_id, 'turn_id': turn_id,
                       'operation_id': operation_id, 'status': 'open', 'sequence': 0,
                       'budget': deepcopy(budget),
                       'acknowledged_sequence': 0, 'final_version_id': None}
            self.repository[turn_id] = doc
            return deepcopy(doc)
        return self._locked(turn_id, run)

    @staticmethod
    def _instruction_key(turn_id, sequence):
        return 'research-instruction:' + sha256(turn_id.encode('utf-8')).hexdigest() + ':' + str(sequence)

    def read(self, account_id, topic_id, turn_id):
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            doc['instructions'] = [deepcopy(self.repository[self._instruction_key(turn_id, sequence)])
                                   for sequence in range(1, doc['sequence'] + 1)]
            return doc
        return self._locked(turn_id, run)

    def append(self, account_id, topic_id, turn_id, text, idempotency_key):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise InstructionInvalid('text must contain 1–4000 Unicode code points')
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InstructionInvalid('idempotency key required')
        key_digest = sha256(idempotency_key.encode('utf-8')).hexdigest()
        turn_digest = sha256(turn_id.encode('utf-8')).hexdigest()
        receipt_key = 'research-idempotency:' + turn_digest + ':' + key_digest
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            existing = self.repository.get(receipt_key)
            if existing is not None:
                item = self.repository[self._instruction_key(turn_id, existing['sequence'])]
                if item['text'] != text:
                    raise InstructionConflict('idempotency key reused with different text')
                return {k: deepcopy(v) for k, v in item.items() if k != 'account_id'}, False
            if doc['status'] not in ('open', 'awaiting_user'):
                raise InstructionConflict('turn closed')
            doc['sequence'] += 1
            item = {'instruction_id': str(uuid4()), 'turn_id': turn_id, 'sequence': doc['sequence'],
                    'text': text, 'received_at': datetime.now(timezone.utc).isoformat(),
                    'operation_id': doc['operation_id'], 'account_id': account_id}
            self.repository[self._instruction_key(turn_id, doc['sequence'])] = item
            self.repository[receipt_key] = {'sequence': doc['sequence'], 'account_id': account_id}
            self.repository[turn_id] = doc
            return {k: deepcopy(v) for k, v in item.items() if k != 'account_id'}, True
        return self._locked(turn_id, run)

    def wait_for_user(self, account_id, topic_id, turn_id, operation_id, expected_sequence, clarification, *, persist=None):
        """Park a settled checkpoint only if no instruction or stop won the race."""
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            if doc['status'] != 'open' or doc['operation_id'] != operation_id or doc['sequence'] != expected_sequence:
                return False
            if persist is not None:
                persist()
            doc['status'] = 'awaiting_user'
            doc['clarification'] = deepcopy(clarification)
            doc['clarification_sequence'] = expected_sequence
            self.repository[turn_id] = doc
            return True
        return self._locked(turn_id, run)

    def accept_findings(self, account_id, topic_id, turn_id, operation_id, version_id):
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            if doc['status'] == 'sealed' and doc['operation_id'] == operation_id and doc['final_version_id'] == version_id:
                return doc
            if (doc['status'] != 'awaiting_user' or doc['operation_id'] != operation_id
                    or doc['sequence'] != doc.get('clarification_sequence')):
                raise InstructionConflict('turn changed while awaiting clarification')
            doc['status'] = 'sealed'
            doc['final_version_id'] = version_id
            doc.pop('clarification', None)
            self.repository[turn_id] = doc
            return deepcopy(doc)
        return self._locked(turn_id, run)

    def awaiting_receipt(self, account_id, topic_id, turn_id, operation_id, sequence):
        def update():
            doc = self._get(account_id, topic_id, turn_id)
            if doc['operation_id'] != operation_id or not 0 <= sequence <= doc['sequence']:
                raise InstructionConflict('stale operation or invalid sequence')
            if sequence != doc.get('awaiting_sequence', 0):
                self.repository[turn_id] = {**doc, 'awaiting_sequence': sequence}
        self._locked(turn_id, update)

    def ack(self, account_id, topic_id, turn_id, operation_id, sequence):
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            if doc['operation_id'] != operation_id or not isinstance(sequence, int) or sequence < 0 or sequence > doc['sequence']:
                raise InstructionConflict('stale operation or invalid sequence')
            doc['acknowledged_sequence'] = max(doc['acknowledged_sequence'], sequence)
            self.repository[turn_id] = doc
            return deepcopy(doc)
        return self._locked(turn_id, run)

    def seal(self, account_id, topic_id, turn_id, expected_sequence, *, operation_id, persist=None, final_version_id=None):
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            if doc['status'] != 'open' or doc['operation_id'] != operation_id or doc['sequence'] != expected_sequence:
                return False
            if persist is not None:
                try:
                    persist()
                except (InstructionNotFound, InstructionConflict, InstructionInvalid) as exc:
                    # Let the repository roll back any partial writes.
                    raise RuntimeError('delivery persistence failed') from exc
            doc['status'] = 'sealed'
            doc['final_version_id'] = final_version_id
            self.repository[turn_id] = doc
            return True
        return self._locked(turn_id, run)

    def stop(self, account_id, topic_id, turn_id, *, idempotency_key=None):
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            receipt_key = None
            if idempotency_key:
                digest = sha256((turn_id + '\0' + idempotency_key).encode()).hexdigest()
                receipt_key = 'research-stop:' + digest
                receipt = self.repository.get(receipt_key)
                if receipt is not None:
                    return deepcopy(receipt)
            if doc['status'] == 'sealed':
                raise InstructionConflict('turn sealed')
            doc['status'] = 'stopped'
            self.repository[turn_id] = doc
            if receipt_key:
                self.repository[receipt_key] = {'operation_id': doc['operation_id'], 'account_id': account_id,
                                                'topic_id': topic_id, 'turn_id': turn_id, 'status': 'stopped'}
            return deepcopy(doc)
        return self._locked(turn_id, run)

    def claim_action(self, account_id, topic_id, turn_id, operation_id, action_id, expected_sequence, *, local_read=False):
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            if doc['status'] != 'open' or doc['operation_id'] != operation_id or doc['sequence'] != expected_sequence:
                return False
            # The action ledger is separate from the instruction document: each
            # tool start writes one small record instead of rewriting the log.
            digest = sha256((str(len(turn_id)) + ':' + turn_id + action_id).encode('utf-8')).hexdigest()
            key = 'research-action:' + digest
            prior = self.repository.get(key)
            if prior and not (local_read and prior.get('local_read') and prior['operation_id'] != operation_id):
                return False
            self.repository[key] = {'account_id': account_id, 'turn_id': turn_id, 'action_id': action_id,
                                    'operation_id': operation_id, 'sequence': expected_sequence, 'local_read': local_read}
            return True
        return self._locked(turn_id, run)

    def action_status(self, account_id, topic_id, turn_id, operation_id, action_id, expected_sequence):
        """Distinguish an unknown prior external call from a stale plan.

        A claimed action without a separately recorded tool result is unknown;
        callers must reconcile it and must never treat it as a skipped call.
        """
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            digest = sha256((str(len(turn_id)) + ':' + turn_id + action_id).encode('utf-8')).hexdigest()
            prior = self.repository.get('research-action:' + digest)
            if prior is not None and not prior.get('local_read'):
                return 'claimed_unknown'
            if doc['status'] == 'stopped':
                return 'stopped'
            if doc['status'] != 'open' or doc['operation_id'] != operation_id:
                return 'superseded'
            if doc['sequence'] != expected_sequence:
                return 'instruction_changed'
            return 'unclaimed'
        return self._locked(turn_id, run)

    def fenced(self, account_id, topic_id, turn_id, operation_id, callback, *, allow_sealed=False, allow_waiting=False):
        def run():
            doc = self._get(account_id, topic_id, turn_id)
            if doc['operation_id'] != operation_id or (doc['status'] != 'open' and not (allow_sealed and doc['status'] == 'sealed') and not (allow_waiting and doc['status'] == 'awaiting_user')):
                raise InstructionConflict('stale operation or turn closed')
            try:
                return callback()
            except (InstructionNotFound, InstructionConflict, InstructionInvalid) as exc:
                raise RuntimeError('fenced callback failed') from exc
        return self._locked(turn_id, run)
