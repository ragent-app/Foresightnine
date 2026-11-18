"""Offline checks only; these do not start an API or submit any model call."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('runner359', Path(__file__).with_name('runner.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_marker_is_exclusive(tmp_path):
    marker = tmp_path / 'run-started.json'
    runner.create_marker(marker, {'state': 'started'})
    with pytest.raises(FileExistsError):
        runner.create_marker(marker, {'state': 'replay'})
    assert 'started' in marker.read_text()


def clean():
    return ({'model_ledger': {'reserved_calls': 0}, 'unresolved_stage_count': 0},
            {'recovery_pending': [], 'runtime_checkpoint': {'pending': None, '_accounting': {'model_call_receipts': {'c': {'settlement': 'settled'}}}}})


def test_confirmed_settlement_is_safe():
    assert runner.reconciliation_safe(*clean())


@pytest.mark.parametrize('kind', ['model', 'model_returned', 'search', 'acquire', 'tool_batch'])
def test_any_pending_checkpoint_keeps_isolation(kind):
    settlement, diagnostics = clean()
    diagnostics['runtime_checkpoint']['pending'] = {'kind': kind}
    assert not runner.reconciliation_safe(settlement, diagnostics)


def test_reserved_or_unknown_receipt_keeps_isolation():
    settlement, diagnostics = clean()
    settlement['model_ledger']['reserved_calls'] = 1
    assert not runner.reconciliation_safe(settlement, diagnostics)
    settlement['model_ledger']['reserved_calls'] = 0
    diagnostics['runtime_checkpoint']['_accounting']['model_call_receipts']['c']['settlement'] = 'unknown'
    assert not runner.reconciliation_safe(settlement, diagnostics)
    diagnostics['recovery_pending'] = [{'kind': 'search'}]
    assert not runner.reconciliation_safe(settlement, diagnostics)
