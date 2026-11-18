"""Provisional deterministic calculation baseline, not a research agent."""
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

HERE = Path(__file__).parent


def load(name):
    data = (HERE / name).read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def percent(numerator, denominator):
    return str((Decimal(numerator) * 100 / Decimal(denominator)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def run():
    started = time.perf_counter()
    initial, initial_hash = load("msft-fy2023.json")
    r = initial["revenue"]
    result_initial = {
        "source": initial["source"], "as_of": initial["as_of"], "unit": initial["unit"],
        "revenue_2022": r["2022"], "revenue_2023": r["2023"],
        "revenue_growth_percent": percent(r["2023"] - r["2022"], r["2022"]),
        "operating_margin_percent": percent(initial["operating_income"]["2023"], r["2023"]),
        "limitations": ["No interpretation, counterargument, source-fetching, or verification was performed."]
    }
    # The later snapshot is accessed only after the initial result is constructed.
    update, update_hash = load("msft-fy2024.json")
    new_revenue = update["revenue"]
    result_update = {
        "source": update["source"], "as_of": update["as_of"], "unit": update["unit"],
        "revenue_2023_unchanged": new_revenue["2023"] == r["2023"],
        "revenue_2024": new_revenue["2024"],
        "revenue_growth_percent": percent(new_revenue["2024"] - new_revenue["2023"], new_revenue["2023"]),
        "operating_margin_percent": percent(update["operating_income"]["2024"], new_revenue["2024"]),
        "limitations": ["Historical replay, not real-time detection; no interpretation or counterargument."]
    }
    return {
        "status": "provisional_calculation_smoke_run_not_formal_comparison",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "local deterministic arithmetic; Python Decimal, two decimals, half up; one run; no retries",
        "source_snapshot_hashes": {"initial": initial_hash, "update": update_hash},
        "model": None, "model_calls": 0, "network_calls": 0,
        "provider_charges_incurred": 0, "compute_and_storage_cost": "not measured",
        "duration_seconds": round(time.perf_counter() - started, 6),
        "initial": result_initial, "update": result_update,
    }


if __name__ == "__main__":
    output = run()
    path = HERE / "fixed-workflow-run-2026-11-11.json"
    with path.open("x") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(output, indent=2))
