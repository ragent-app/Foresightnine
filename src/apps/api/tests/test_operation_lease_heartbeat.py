"""Short-lease tests: active worker renews, abandoned leases remain recoverable."""
from datetime import timedelta
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from pydantic import BaseModel
from app.operations import OperationManager
from app.operation_store import InMemoryOperationStore
from app.providers import RuntimeClock


class Result(BaseModel):
    value: str


def test_active_worker_renews_and_abandoned_lease_expires():
    store=InMemoryOperationStore(); clock=RuntimeClock()
    manager=OperationManager(clock,store=store,lease_seconds=1)
    entered=Event(); release=Event()
    def build(_operation_id):
        entered.set()
        assert release.wait(5)
        return Result(value='ok'),'result'
    receipt,_=manager.submit_async(endpoint='/v1/lease-test',idempotency_key='active',
        owner_subject='account:owner',request_payload={},result_field='result',build_result=build)
    operation=receipt.operation.operation_id
    assert entered.wait(2)
    try:
        assert not store.renew_lease(operation,lease_owner='other',now=clock.now(),
                                     lease_expires_at=clock.now()+timedelta(seconds=10))
        Event().wait(1.3)
        assert manager.recover_expired_leases(now=clock.now())==0
        assert store.get_operation(operation)['status']=='running'
    finally:
        release.set()
    end=monotonic()+3
    while monotonic()<end and store.get_operation(operation)['status']=='running': sleep(.01)
    assert store.get_operation(operation)['status']=='completed'
    assert not store.renew_lease(operation,lease_owner=manager.lease_owner,now=clock.now(),
                                 lease_expires_at=clock.now()+timedelta(seconds=10))
    abandoned=manager.start(endpoint='/v1/abandoned',idempotency_key='abandoned',owner_subject='account:owner')
    future=clock.now()+timedelta(seconds=2)
    assert not store.renew_lease(abandoned.operation_id,lease_owner=manager.lease_owner,now=future,
                                 lease_expires_at=future+timedelta(seconds=1))
    assert manager.recover_expired_leases(now=future)==1
    assert store.get_operation(abandoned.operation_id)['status']=='failed'
    assert not store.renew_lease(abandoned.operation_id,lease_owner=manager.lease_owner,now=future,
                                 lease_expires_at=future+timedelta(seconds=1))
