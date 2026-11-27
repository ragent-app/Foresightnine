"""Controlled V4a Full-Stack Live Evaluation Harness (6-Bank + 14-Issuer + Concurrency).

Implements D8 Live Verification Gates against a running ForesightNine API instance:
  Gate 1: 14-issuer 0-LLM applicability & disclosure probe (/v2/research/banks/analyze + /segments)
  Gate 2: V2 6-bank peer comparison (dual TBVPS toggle), mixed non-bank guard & Live Layer 2 Brief
  Gate 3: All 6 US Major Banks Live Direction A (Earnings Squad) + V3a bank call segment verification
  Gate 4: BLK post-GS SIC 6211 cache-poisoning control (422 + is_gated=True)
  Gate 5: Direction B bank relative watch (peer_cet1_buffer_bps)
  Gate 6: 4-way concurrent /v2/research/banks/analyze stress & cgroup memory peak check (< 550 MiB)

Supports ``--resume`` with atomic per-gate / per-bank JSON checkpointing so interrupted runs
never lose already-completed multi-minute LLM bank runs. Registers an ephemeral local test account
and deletes it in ``finally:`` so zero probe rows remain in Postgres.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4

import httpx


MAJOR_BANKS = ["JPM", "BAC", "WFC", "C", "GS", "MS"]
CUSTODIAN_AND_REGIONAL_BANKS = ["BNY", "STT", "USB", "PNC"]
NEGATIVE_CONTROLS = ["BLK", "AXP", "PGR", "PLD"]

EXPECTED_SUBTYPES = {
    "JPM": "universal_commercial",
    "BAC": "universal_commercial",
    "WFC": "universal_commercial",
    "C": "universal_commercial",
    "GS": "investment_bank_bhc",
    "MS": "investment_bank_bhc",
}


# SEC-filed company financial supplement, 3Q26, rows "Tangible book value
# per common share" and "Return on average tangible common equity".
REFERENCE_3Q26 = {
    "STT": {"tbvps": "59.69", "rotce": "25.1",
            "source": "https://www.sec.gov/Archives/edgar/data/93751/000009375126000592/exhibit992-3q26earningsrel.htm"},
}

def _finite_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


class EvalFailure(AssertionError):
    """Raised when a pre-registered D8 gate assertion fails."""


def _log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        pass


def _save_summary(output_json: str, summary: dict[str, Any]) -> None:
    if not output_json:
        return
    out_path = Path(output_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp_path, out_path)


def _req(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> httpx.Response:
    return client.request(
        method,
        path,
        headers=headers,
        json=json_body,
        params=params,
        timeout=timeout,
    )


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise EvalFailure(msg)


def _audit_bank_values(sym: str, data: dict[str, Any]) -> dict[str, Any]:
    """Check safety independently of completeness; unavailable results are valid."""
    disc = data.get("disclosure") or {}
    bridge = disc.get("tbvps_bridge") or {}
    diag = data.get("diagnostics") or {}
    val = data.get("valuation") or {}
    rep = _finite_decimal(bridge.get("tbvps_reported_usd"))
    std = _finite_decimal(bridge.get("tbvps_xbrl_std_usd"))
    current_schema = disc.get("schema_version") == "v4a.3"
    rep_ok = current_schema and bridge.get("tbvps_reported_verified") is True and rep is not None and rep > 0
    std_ok = current_schema and bridge.get("tbvps_xbrl_std_verified") is True and std is not None and std > 0
    rot_ok = current_schema and disc.get("rotce_q_verified") is True and _finite_decimal(disc.get("rotce_q_reported_pct")) is not None
    mode = val.get("primary_tbvps_basis")
    basis_ok = rep_ok if mode == "reported_market_convention" else std_ok if mode in ("xbrl_standardized", "xbrl_std_fallback") else False
    price = _finite_decimal(val.get("price_usd"))
    price_ok = price is not None and price > 0
    for field in ("p_to_tbv_primary", "p_to_tbv_xbrl_std", "implied_rotce_pct", "current_rotce_pct", "rotce_spread_vs_implied_pp", "justified_p_to_tbv_at_current_rotce"):
        raw = val.get(field)
        _assert(raw is None or _finite_decimal(raw) is not None, f"{sym} non-finite {field}")
    ptbv = _finite_decimal(val.get("p_to_tbv_primary"))
    implied = _finite_decimal(val.get("implied_rotce_pct"))
    current = _finite_decimal(val.get("current_rotce_pct"))
    justified = _finite_decimal(val.get("justified_p_to_tbv_at_current_rotce"))
    spread = _finite_decimal(val.get("rotce_spread_vs_implied_pp"))
    _assert(ptbv is None or (basis_ok and price_ok), f"{sym} P/TBV has unverified/non-positive inputs")
    _assert(val.get("p_to_tbv_xbrl_std") is None or (std_ok and price_ok), f"{sym} standardized P/TBV has unverified/non-positive inputs")
    _assert(implied is None or ptbv is not None, f"{sym} implied ROTCE lacks market P/TBV")
    _assert(current is None or rot_ok, f"{sym} current ROTCE is unverified")
    _assert(justified is None or rot_ok, f"{sym} justified P/TBV lacks verified ROTCE")
    _assert(spread is None or (implied is not None and current is not None), f"{sym} spread lacks verified inputs")
    _assert(diag.get("rotce_q_pct") is None or rot_ok, f"{sym} diagnostic ROTCE is unverified")
    cells = val.get("sensitivity_matrix") or []
    _assert(not cells or len(cells) == 9, f"{sym} malformed sensitivity matrix")
    for cell in cells:
        for field, allowed in (("implied_rotce_pct", ptbv is not None),
                               ("justified_p_to_tbv", rot_ok),
                               ("rotce_spread_pp", ptbv is not None and rot_ok)):
            raw = cell.get(field)
            _assert(raw is None or (_finite_decimal(raw) is not None and allowed), f"{sym} invalid sensitivity {field}")
    reference = REFERENCE_3Q26.get(sym)
    reference_status = "not_registered"
    if reference:
        if disc.get("period_end_date") != "2026-09-30" or not rep_ok or not rot_ok:
            reference_status = "unavailable"
        else:
            _assert(abs(rep - Decimal(reference["tbvps"])) <= Decimal("0.01"), f"{sym} reported TBVPS disagrees with SEC checkpoint")
            actual_rotce = _finite_decimal(disc.get("rotce_q_reported_pct"))
            _assert(abs(actual_rotce - Decimal(reference["rotce"])) <= Decimal("0.01"), f"{sym} ROTCE disagrees with SEC checkpoint")
            reference_status = "matched"
    return {"tbvps": basis_ok, "rotce": rot_ok, "market_valuation": ptbv is not None,
            "sensitivity": bool(cells) and all(_finite_decimal(c.get("implied_rotce_pct")) is not None and _finite_decimal(c.get("justified_p_to_tbv")) is not None and _finite_decimal(c.get("rotce_spread_pp")) is not None for c in cells),
            "reference_status": reference_status, "reference_source": reference.get("source") if reference else None}


def read_container_memory(container: str = "foresightnine-api-1") -> dict[str, Any]:
    """Best-effort read of container cgroup memory.current and memory.peak (MiB)."""
    cmd = [
        "sudo",
        "docker",
        "exec",
        container,
        "sh",
        "-c",
        "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak 2>/dev/null || true",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip().isdigit()]
        if len(lines) >= 2:
            curr_b, peak_b = int(lines[0]), int(lines[1])
            return {
                "memory_current_mib": round(curr_b / (1024 * 1024), 2),
                "memory_peak_mib": round(peak_b / (1024 * 1024), 2),
            }
        if len(lines) == 1:
            return {"memory_current_mib": round(int(lines[0]) / (1024 * 1024), 2)}
    except Exception as exc:
        return {"error": str(exc)}
    return {}


def register_ephemeral_account(client: httpx.Client) -> tuple[str, dict[str, str], str]:
    suffix = uuid4().hex[:10]
    email = f"v4a-eval-{suffix}@example.test"
    password = f"V4aEval!{suffix}"
    resp = _req(
        client,
        "POST",
        "/v1/auth/local/register",
        json_body={"email": email, "password": password},
        timeout=30.0,
    )
    _assert(resp.status_code == 201, f"Local register failed: {resp.status_code} {resp.text}")
    payload = resp.json()
    token = payload["access_token"]
    account_id = payload["account"]["account_id"]
    return account_id, {"Authorization": f"Bearer {token}"}, email


def delete_ephemeral_account(client: httpx.Client, headers: dict[str, str]) -> dict[str, Any]:
    try:
        resp = _req(client, "DELETE", "/v1/auth/local/account", headers=headers, timeout=30.0)
        return {"status_code": resp.status_code, "ok": resp.status_code in (200, 204)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_gate_1_14_issuer_probe(
    client: httpx.Client,
    headers: dict[str, str],
) -> dict[str, Any]:
    _log("\n=== Gate 1: 14-Issuer 0-LLM Bank Applicability & Disclosure Probe ===")
    results: dict[str, Any] = {"qualified": {}, "negative_controls": {}, "segments": {}, "coverage": {}}

    for sym in MAJOR_BANKS + CUSTODIAN_AND_REGIONAL_BANKS:
        t0 = time.perf_counter()
        resp = _req(
            client,
            "POST",
            "/v2/research/banks/analyze",
            headers=headers,
            json_body={
                "symbol": sym,
                "coe_pct": "10.0",
                "terminal_g_pct": "3.0",
                "tbvps_basis": "reported_market_convention",
            },
            timeout=120.0,
        )
        elapsed = round(time.perf_counter() - t0, 3)
        _assert(
            resp.status_code == 200,
            f"Gate 1 failed for {sym}: status={resp.status_code} body={resp.text[:400]}",
        )
        data = resp.json()
        qual = data.get("qualification") or {}
        disc = data.get("disclosure") or {}
        bridge = disc.get("tbvps_bridge") or {}
        diag = data.get("diagnostics") or {}
        val = data.get("valuation") or {}

        _assert(qual.get("is_bank_v4a") is True, f"{sym} expected is_bank_v4a=True, got {qual}")
        _assert(
            qual.get("active_sector_plugin") == "us_large_bank_v4a",
            f"{sym} expected active_sector_plugin=us_large_bank_v4a, got {qual.get('active_sector_plugin')}",
        )
        if sym in EXPECTED_SUBTYPES:
            _assert(
                qual.get("bank_subtype") == EXPECTED_SUBTYPES[sym],
                f"{sym} bank_subtype mismatch: expected {EXPECTED_SUBTYPES[sym]}, got {qual.get('bank_subtype')}",
            )
        _assert(
            diag.get("bank_quality_light") in ("red", "yellow", "green"),
            f"{sym} invalid bank_quality_light: {diag.get('bank_quality_light')}",
        )
        _assert(len(diag.get("metrics") or {}) >= 5, f"{sym} expected >= 5 diagnostic metrics")
        results["coverage"][sym] = _audit_bank_values(sym, data)

        seg_resp = _req(
            client,
            "GET",
            "/v2/research/banks/segments",
            headers=headers,
            params={"symbol": sym},
            timeout=60.0,
        )
        _assert(
            seg_resp.status_code == 200,
            f"Gate 1 segments failed for {sym}: {seg_resp.status_code} {seg_resp.text[:300]}",
        )
        seg_data = seg_resp.json()
        op_segs = seg_data.get("operating_segments") or []
        results["segments"][sym] = {
            "operating_segment_count": len(op_segs),
            "reconciliation_status": seg_data.get("reconciliation_status"),
            "segments_basis": seg_data.get("segments_basis"),
            "first_segment": op_segs[0] if op_segs else None,
            "raw_segments": seg_data,
        }
        results["qualified"][sym] = {
            "elapsed_s": elapsed,
            "bank_subtype": qual.get("bank_subtype"),
            "sic": qual.get("sic"),
            "bank_quality_light": diag.get("bank_quality_light"),
            "cet1_standardized_pct": disc.get("cet1_standardized_pct"),
            "cet1_reg_min_pct": disc.get("cet1_reg_min_pct"),
            "cet1_buffer_bps": diag.get("cet1_buffer_bps"),
            "tbvps_reported_usd": bridge.get("tbvps_reported_usd"),
            "tbvps_reported_source": bridge.get("tbvps_reported_source"),
            "tbvps_xbrl_std_usd": bridge.get("tbvps_xbrl_std_usd"),
            "tbvps_gap_pct": bridge.get("tbvps_gap_pct"),
            "rotce_q_pct": diag.get("rotce_q_pct"),
            "htm_loss_pct_of_tce": diag.get("htm_loss_pct_of_tce"),
            "pro_forma_cet1_ex_htm_pct": diag.get("pro_forma_cet1_ex_htm_pct"),
            "price_usd": val.get("price_usd"),
            "p_to_tbv_primary": val.get("p_to_tbv_primary"),
            "justified_p_to_tbv_at_current_rotce": val.get("justified_p_to_tbv_at_current_rotce"),
            "implied_rotce_pct": val.get("implied_rotce_pct"),
            "segment_count": len(op_segs),
        }
        _log(
            f"  [PASS] {sym:4s} ({qual.get('bank_subtype'):24s}) light={diag.get('bank_quality_light'):6s} "
            f"CET1={disc.get('cet1_standardized_pct')}% buf={diag.get('cet1_buffer_bps')}bps "
            f"TBVPS=${bridge.get('tbvps_reported_usd')}/${bridge.get('tbvps_xbrl_std_usd')} "
            f"P/TBV={val.get('p_to_tbv_primary')}x vs Fair={val.get('justified_p_to_tbv_at_current_rotce')}x "
            f"segs={len(op_segs)} ({elapsed}s)"
        )

    for sym in NEGATIVE_CONTROLS:
        t0 = time.perf_counter()
        resp = _req(
            client,
            "POST",
            "/v2/research/banks/analyze",
            headers=headers,
            json_body={"symbol": sym},
            timeout=90.0,
        )
        elapsed = round(time.perf_counter() - t0, 3)
        _assert(
            resp.status_code == 422,
            f"Negative control {sym} expected 422, got {resp.status_code}: {resp.text[:300]}",
        )
        results["negative_controls"][sym] = {
            "status_code": resp.status_code,
            "elapsed_s": elapsed,
            "detail": resp.json() if resp.headers.get("content-type", "").startswith("application/") else resp.text,
        }
        _log(f"  [PASS] {sym:4s} negative control -> HTTP 422 ({elapsed}s)")

    return results


def run_gate_2_v2_peer_compare(
    client: httpx.Client,
    headers: dict[str, str],
) -> dict[str, Any]:
    _log("\n=== Gate 2: V2 6-Bank Peer Compare, TBVPS Toggle, Mixed Guard & Live Brief ===")
    t0 = time.perf_counter()
    rep_resp = _req(
        client,
        "POST",
        "/v2/research/peers/compare",
        headers=headers,
        json_body={
            "primary_symbol": "JPM",
            "symbols": MAJOR_BANKS,
            "tbvps_basis": "reported_market_convention",
            "bank_coe_pct": "10.0",
            "bank_terminal_g_pct": "3.0",
        },
        timeout=180.0,
    )
    _assert(rep_resp.status_code == 200, f"V2 bank compare reported failed: {rep_resp.status_code} {rep_resp.text[:400]}")
    rep = rep_resp.json()
    _assert(rep.get("comparison_mode") == "us_large_banks", f"Expected us_large_banks mode, got {rep.get('comparison_mode')}")
    _assert(len(rep.get("bank_companies") or []) == 6, f"Expected 6 bank_companies, got {len(rep.get('bank_companies') or [])}")
    medians = rep.get("bank_peer_medians") or {}
    for metric in ("p_to_tbv_primary", "rotce_q_pct", "implied_rotce_pct"):
        coverage = sum(_finite_decimal(row.get(metric)) is not None for row in rep.get("bank_companies") or [])
        _assert(coverage >= 2 and _finite_decimal(medians.get(metric)) is not None,
                f"{metric} needs at least two comparable observations and a finite median; got {coverage}")

    xbrl_resp = _req(
        client,
        "POST",
        "/v2/research/peers/compare",
        headers=headers,
        json_body={
            "primary_symbol": "JPM",
            "symbols": MAJOR_BANKS,
            "tbvps_basis": "xbrl_standardized",
            "bank_coe_pct": "10.0",
            "bank_terminal_g_pct": "3.0",
        },
        timeout=180.0,
    )
    _assert(xbrl_resp.status_code == 200, f"V2 bank compare xbrl_standardized failed: {xbrl_resp.status_code} {xbrl_resp.text[:400]}")
    xbrl_mat = xbrl_resp.json()
    _assert(xbrl_mat.get("comparison_mode") == "us_large_banks", "Expected us_large_banks in xbrl_standardized compare")
    _assert(
        xbrl_mat.get("tbvps_display_mode") == "xbrl_standardized",
        f"Expected tbvps_display_mode=xbrl_standardized, got {xbrl_mat.get('tbvps_display_mode')}",
    )
    xbrl_bases = {c.get("symbol"): c.get("tbvps_basis") for c in (xbrl_mat.get("bank_companies") or [])}
    _assert(
        xbrl_bases.get("JPM") == "xbrl_standardized",
        f"Expected JPM tbvps_basis=xbrl_standardized, got {xbrl_bases.get('JPM')}",
    )

    mixed_resp = _req(
        client,
        "POST",
        "/v2/research/peers/compare",
        headers=headers,
        json_body={"primary_symbol": "JPM", "symbols": ["JPM", "NVDA"]},
        timeout=180.0,
    )
    _assert(mixed_resp.status_code == 200, f"Mixed JPM+NVDA compare failed: {mixed_resp.status_code} {mixed_resp.text[:400]}")
    mixed_mat = mixed_resp.json()
    _assert(
        mixed_mat.get("comparison_mode") != "us_large_banks",
        f"Mixed JPM+NVDA must NOT stay in us_large_banks mode, got {mixed_mat.get('comparison_mode')}",
    )
    _assert(
        bool(mixed_mat.get("comparability_warnings")),
        f"Mixed JPM+NVDA must emit comparability_warnings, got {mixed_mat.get('comparability_warnings')}",
    )

    brief_t0 = time.perf_counter()
    brief_resp = _req(
        client,
        "POST",
        "/v2/research/peers/compare/brief",
        headers=headers,
        json_body={"comparison_id": rep["comparison_id"]},
        timeout=180.0,
    )
    brief_elapsed = round(time.perf_counter() - brief_t0, 3)
    _assert(brief_resp.status_code == 200, f"Peer brief failed: {brief_resp.status_code} {brief_resp.text[:400]}")
    brief = brief_resp.json()
    _assert(bool((brief.get("summary") or "").strip()), "Brief summary is empty")
    _assert(bool(brief.get("cited_metrics")), "Brief cited_metrics is empty")

    _log(
        f"  [PASS] 6-bank compare OK ({len(rep['bank_companies'])} banks, medians={list((rep.get('bank_peer_medians') or {}).keys())[:4]}), "
        f"TBVPS toggle OK, mixed JPM+NVDA guarded ({mixed_mat.get('comparison_mode')}), "
        f"Live Brief OK ({brief_elapsed}s, {len(brief.get('cited_metrics') or [])} citations)"
    )
    return {
        "total_elapsed_s": round(time.perf_counter() - t0, 3),
        "comparison_id": rep["comparison_id"],
        "bank_peer_medians": rep.get("bank_peer_medians"),
        "mixed_warnings": mixed_mat.get("comparability_warnings"),
        "brief_elapsed_s": brief_elapsed,
        "brief_summary_preview": (brief.get("summary") or "")[:300],
        "brief_cited_metrics_count": len(brief.get("cited_metrics") or []),
    }


def _run_earnings_squad_for_symbol(
    client: httpx.Client,
    headers: dict[str, str],
    sym: str,
    question: str,
    *,
    poll_timeout_s: float = 2700.0,
) -> dict[str, Any]:
    t_topic = _req(
        client,
        "POST",
        "/v2/research/topics",
        headers=headers,
        json_body={"question": question},
        timeout=30.0,
    )
    _assert(t_topic.status_code in (200, 201), f"Topic creation failed for {sym}: {t_topic.status_code} {t_topic.text}")
    topic_id = t_topic.json()["topic_id"]

    t0 = time.perf_counter()
    run_resp = _req(
        client,
        "POST",
        f"/v2/research/topics/{topic_id}/earnings-squad/run",
        headers=headers,
        json_body={
            "symbol": sym,
            "question": question,
            "execution_mode": "async",
        },
        timeout=60.0,
    )
    _assert(run_resp.status_code in (200, 202), f"Squad start failed for {sym}: {run_resp.status_code} {run_resp.text[:500]}")
    body = run_resp.json()
    deadline = time.monotonic() + poll_timeout_s
    last_stages: list[str] = list(body.get("checkpoint_completed_stages") or [])
    last_beat = time.monotonic()
    while body.get("status") not in ("completed", "failed", "cancelled"):
        if time.monotonic() >= deadline:
            raise EvalFailure(
                f"Squad {sym} timed out after {poll_timeout_s}s; last_status={body.get('status')} "
                f"stages={body.get('checkpoint_completed_stages')} events={len(body.get('org_events') or [])}"
            )
        time.sleep(3.0)
        dos_resp = _req(
            client,
            "GET",
            f"/v2/research/topics/{topic_id}/earnings-squad/dossier",
            headers=headers,
            timeout=30.0,
        )
        _assert(dos_resp.status_code == 200, f"Poll dossier for {sym} failed: {dos_resp.status_code} {dos_resp.text[:300]}")
        body = dos_resp.json()
        cur_stages = list(body.get("checkpoint_completed_stages") or [])
        if cur_stages != last_stages or (time.monotonic() - last_beat) >= 30.0:
            last_stages = cur_stages
            last_beat = time.monotonic()
            _log(
                f"    [progress] {sym:4s} {time.perf_counter() - t0:6.1f}s status={body.get('status')} "
                f"stages={cur_stages} events={len(body.get('org_events') or [])}"
            )

    elapsed = round(time.perf_counter() - t0, 3)
    _assert(body.get("status") == "completed", f"Squad {sym} status={body.get('status')} error={body.get('error')}")
    dossier = body.get("dossier") or {}
    events = body.get("org_events") or []
    first_verified_s = None
    if events:
        first = datetime.fromisoformat(events[0]["timestamp"])
        verified = next((event for event in events if event.get("stage") == "verified_evidence_ready"), None)
        if verified:
            first_verified_s = (datetime.fromisoformat(verified["timestamp"]) - first).total_seconds()
    return {
        "topic_id": topic_id,
        "elapsed_s": elapsed,
        "first_verified_evidence_s": first_verified_s,
        "run_id": body.get("run_id"),
        "status": body.get("status"),
        "dossier": dossier,
    }


def _poll_call_audit(
    client: httpx.Client,
    headers: dict[str, str],
    resp: httpx.Response,
    *,
    timeout_s: float = 240.0,
) -> dict[str, Any]:
    _assert(resp.status_code in (200, 202), f"Call audit start failed: {resp.status_code} {resp.text[:400]}")
    data = resp.json()
    if resp.status_code == 200 and data.get("report") is not None:
        return data
    op_id = data.get("operation_id")
    _assert(bool(op_id), f"Call audit 202 missing operation_id: {data}")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        op_resp = _req(
            client,
            "GET",
            f"/v2/research/call-audit/operations/{op_id}",
            headers=headers,
            timeout=30.0,
        )
        _assert(op_resp.status_code == 200, f"Poll call audit op {op_id} failed: {op_resp.status_code}")
        op = op_resp.json()
        if op.get("status") in ("completed", "failed", "cancelled"):
            _assert(op.get("status") == "completed", f"Call audit failed: {op.get('error')}")
            return op
        time.sleep(2.0)
    raise EvalFailure(f"Call audit operation {op_id} timed out after {timeout_s}s")


def _validate_and_summarize_bank_squad(sym: str, res: dict[str, Any]) -> dict[str, Any]:
    dossier = res["dossier"]
    sec_app = dossier.get("sector_applicability") or {}
    snap = dossier.get("snapshot") or {}
    usage = dossier.get("slot_usage_summary") or {}
    cards = dossier.get("evidence_cards") or []
    bears = dossier.get("bear_cases") or []

    _assert(
        sec_app.get("active_sector_plugin") == "us_large_bank_v4a",
        f"{sym} Squad sector_applicability.active_sector_plugin={sec_app.get('active_sector_plugin')}",
    )
    _assert(
        dossier.get("selected_l2_pack_id") == "kpi_us_large_banks",
        f"{sym} Squad selected_l2_pack_id={dossier.get('selected_l2_pack_id')}",
    )
    _assert(snap.get("bank_diagnostics") is not None, f"{sym} Squad missing snapshot.bank_diagnostics")
    _assert(snap.get("bank_valuation") is not None, f"{sym} Squad missing snapshot.bank_valuation")
    _assert(len(cards) > 0, f"{sym} Squad returned 0 evidence_cards")
    _assert(bool((dossier.get("memo_markdown") or "").strip()), f"{sym} Squad returned empty memo_markdown")

    entry = {
        "topic_id": res["topic_id"],
        "elapsed_s": res["elapsed_s"],
        "first_verified_evidence_s": res["first_verified_evidence_s"],
        "selected_l2_pack_id": dossier.get("selected_l2_pack_id"),
        "active_sector_plugin": sec_app.get("active_sector_plugin"),
        "is_gated": sec_app.get("is_gated"),
        "evidence_cards_count": len(cards),
        "bear_cases_count": len(bears),
        "total_calls": usage.get("total_calls"),
        "total_tokens": usage.get("total_tokens"),
        "stage_usage": usage.get("stages", {}),
    }
    _log(
        f"  [PASS] Squad {sym:4s}: plugin={sec_app.get('active_sector_plugin')} l2={dossier.get('selected_l2_pack_id')} "
        f"cards={len(cards)} bears={len(bears)} calls={usage.get('total_calls')} "
        f"tokens={usage.get('total_tokens')} ({res['elapsed_s']}s)"
    )
    return entry


def run_gate_3_and_4_squad_and_v3a(
    client: httpx.Client,
    headers: dict[str, str],
    gate1_segments: dict[str, Any],
    *,
    check_cgroup: bool = True,
    squad_concurrency: int = 2,
    prior_results: dict[str, Any] | None = None,
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    _log(f"\n=== Gate 3: All 6 Major Banks Live Direction A (concurrency={squad_concurrency}) + V3a Bank Call Verification ===")
    prior = prior_results or {}
    prior_squad = prior.get("squad") if isinstance(prior.get("squad"), dict) else {}
    squad_results: dict[str, Any] = {}
    remaining_syms: list[str] = []

    for sym in MAJOR_BANKS:
        cached = prior_squad.get(sym)
        if (
            isinstance(cached, dict)
            and cached.get("active_sector_plugin") == "us_large_bank_v4a"
            and cached.get("selected_l2_pack_id") == "kpi_us_large_banks"
            and (cached.get("evidence_cards_count") or 0) > 0
        ):
            squad_results[sym] = cached
            _log(
                f"  [SKIP/CACHED] Squad {sym:4s}: cards={cached.get('evidence_cards_count')} "
                f"bears={cached.get('bear_cases_count')} ({cached.get('elapsed_s')}s)"
            )
        else:
            remaining_syms.append(sym)

    out_payload: dict[str, Any] = {
        "squad": squad_results,
        **({"v3a_jpm": prior["v3a_jpm"]} if isinstance(prior.get("v3a_jpm"), dict) else {}),
        **({"blk_post_gs": prior["blk_post_gs"]} if isinstance(prior.get("blk_post_gs"), dict) else {}),
    }

    base_url = str(client.base_url)

    def _run_one_bank(sym: str) -> tuple[str, dict[str, Any]]:
        q = (
            f"Analyze {sym}'s latest SEC banking disclosures: evaluate CET1 regulatory buffer, "
            f"HTM unrealized losses vs TCE, deposit & credit trends, ROTCE vs 10% CoE, and Gordon P/TBV valuation."
        )
        with httpx.Client(base_url=base_url, timeout=300.0) as local_client:
            res = _run_earnings_squad_for_symbol(local_client, headers, sym, q)
        return sym, res

    if remaining_syms:
        if squad_concurrency <= 1:
            for sym in remaining_syms:
                _, res = _run_one_bank(sym)
                squad_results[sym] = _validate_and_summarize_bank_squad(sym, res)
                out_payload["squad"] = {s: squad_results[s] for s in MAJOR_BANKS if s in squad_results}
                if on_checkpoint is not None:
                    on_checkpoint(out_payload)
        else:
            with ThreadPoolExecutor(max_workers=squad_concurrency) as pool:
                futs = [pool.submit(_run_one_bank, sym) for sym in remaining_syms]
                for fut in as_completed(futs):
                    sym, res = fut.result()
                    squad_results[sym] = _validate_and_summarize_bank_squad(sym, res)
                    out_payload["squad"] = {s: squad_results[s] for s in MAJOR_BANKS if s in squad_results}
                    if on_checkpoint is not None:
                        on_checkpoint(out_payload)

    squad_results = {sym: squad_results[sym] for sym in MAJOR_BANKS}
    out_payload["squad"] = squad_results
    gate3_mem = read_container_memory() if check_cgroup else {}
    out_payload["gate3_container_memory"] = gate3_mem
    _log(f"  [INFO] Container memory after Gate 3 6-bank Squad: {gate3_mem}")

    # V3a Bank Call Segment Verification on JPM using actual SEC segment name/revenue from Gate 1
    if isinstance(prior.get("v3a_jpm"), dict) and prior["v3a_jpm"].get("has_bank_v4a_matched") is True:
        out_payload["v3a_jpm"] = prior["v3a_jpm"]
        _log(f"  [SKIP/CACHED] V3a JPM Call Segment Audit: segment={out_payload['v3a_jpm'].get('matched_segment')!r}")
    else:
        jpm_segs = (gate1_segments.get("JPM") or {}).get("raw_segments") or {}
        op_segs = [
            row
            for row in (jpm_segs.get("operating_segments") or [])
            if row.get("display_name") and row.get("net_revenue_usd") is not None
        ]
        _assert(len(op_segs) >= 1, f"JPM has no operating segments with net_revenue_usd in Gate 1: {jpm_segs}")
        seg0 = op_segs[0]
        seg_name = str(seg0["display_name"])
        seg_rev_usd = float(seg0["net_revenue_usd"])
        seg_rev_b = round(seg_rev_usd / 1e9, 2)
        transcript_text = (
            f"JPMorgan Chase & Co. Quarterly Earnings Conference Call\n"
            f"Prepared Remarks\n"
            f"Operator: Good morning everyone, welcome to the JPMorgan Chase quarterly earnings call.\n"
            f"Jeremy Barnum: Thank you. In {seg_name}, segment net revenue reached ${seg_rev_b:.2f} billion "
            f"during the period, reflecting resilient client activity across our core franchise. "
            f"Within our Equities cash trading sub-desk specifically, internal desk revenue reached $1.42 billion "
            f"for the quarter.\n"
            f"Question-and-Answer Session\n"
            f"Analyst (Betsy Graseck): Can you comment on CET1 capital deployment and NII trajectory?\n"
            f"Jeremy Barnum: Our standardized CET1 ratio gives us substantial capital flexibility while we monitor "
            f"rates and credit normalization through year-end.\n"
        )
        v3a_topic_resp = _req(
            client,
            "POST",
            "/v2/research/topics",
            headers=headers,
            json_body={"question": "JPM Q3 Call Audit Verification"},
            timeout=30.0,
        )
        _assert(
            v3a_topic_resp.status_code in (200, 201),
            f"V3a JPM topic creation failed: {v3a_topic_resp.status_code} {v3a_topic_resp.text}",
        )
        v3a_topic_id = v3a_topic_resp.json()["topic_id"]

        src_resp = _req(
            client,
            "POST",
            "/v2/research/sources",
            headers=headers,
            json_body={
                "title": "JPM Synthetic V4a Live Call Transcript",
                "text": transcript_text,
                "kind": "user_supplied",
                "permissions": {"store": True, "model_input": True, "display": True, "export": True},
            },
            timeout=30.0,
        )
        _assert(src_resp.status_code in (200, 201), f"Upload call transcript failed: {src_resp.status_code} {src_resp.text}")
        source_id = src_resp.json()["source_id"]

        t_v3a = time.perf_counter()
        audit_resp = _req(
            client,
            "POST",
            "/v2/research/call-audit/run",
            headers={**headers, "Idempotency-Key": f"v4a-jpm-call-{uuid4().hex[:8]}"},
            json_body={
                "source_ids": [source_id],
                "topic_id": v3a_topic_id,
                "symbol": "JPM",
                "force_refresh": True,
            },
            timeout=180.0,
        )
        audit_done = _poll_call_audit(client, headers, audit_resp, timeout_s=240.0)
        v3a_elapsed = round(time.perf_counter() - t_v3a, 3)
        report = audit_done.get("report") or {}
        ledger = audit_done.get("ledger") or {}
        rows = ledger.get("rows") or report.get("ledger_rows") or report.get("collisions") or report.get("claims") or []
        has_bank_match = "bank_v4a_matched" in json.dumps({"report": report, "ledger": ledger})
        _assert(
            has_bank_match,
            f"Expected at least one bank_v4a_matched segment collision in JPM V3a audit; got rows={str(rows)[:500]}",
        )
        _log(
            f"  [PASS] V3a JPM Call Segment Audit: bank_v4a_matched=True, matched_segment={seg_name!r} (${seg_rev_b}B), "
            f"rows={len(rows)} ({v3a_elapsed}s)"
        )
        out_payload["v3a_jpm"] = {
            "elapsed_s": v3a_elapsed,
            "matched_segment": seg_name,
            "matched_rev_usd": seg_rev_usd,
            "rows_count": len(rows),
            "has_bank_v4a_matched": has_bank_match,
        }
        if on_checkpoint is not None:
            on_checkpoint(out_payload)

    _log("\n=== Gate 4: BLK Post-GS SIC 6211 Cache-Poisoning Control ===")
    if (
        isinstance(prior.get("blk_post_gs"), dict)
        and prior["blk_post_gs"].get("analyze_status") == 422
        and prior["blk_post_gs"].get("is_gated") is True
        and prior["blk_post_gs"].get("active_sector_plugin") is None
    ):
        out_payload["blk_post_gs"] = prior["blk_post_gs"]
        _log(f"  [SKIP/CACHED] BLK post-GS control ({out_payload['blk_post_gs'].get('squad_elapsed_s')}s)")
    else:
        blk_analyze = _req(
            client,
            "POST",
            "/v2/research/banks/analyze",
            headers=headers,
            json_body={"symbol": "BLK"},
            timeout=90.0,
        )
        _assert(blk_analyze.status_code == 422, f"BLK /banks/analyze post-GS expected 422, got {blk_analyze.status_code}")

        blk_squad = _run_earnings_squad_for_symbol(
            client,
            headers,
            "BLK",
            "Analyze BlackRock (BLK) quarterly AUM flows, fee margin, and operating profitability.",
        )
        blk_dossier = blk_squad["dossier"]
        blk_sec = blk_dossier.get("sector_applicability") or {}
        _assert(
            blk_sec.get("active_sector_plugin") is None,
            f"BLK post-GS Squad leaked bank plugin: {blk_sec.get('active_sector_plugin')}",
        )
        _assert(
            blk_sec.get("is_gated") is True,
            f"BLK post-GS Squad expected is_gated=True, got {blk_sec.get('is_gated')}",
        )
        _log(
            f"  [PASS] BLK post-GS control: /banks/analyze -> 422, Squad -> is_gated={blk_sec.get('is_gated')}, "
            f"active_sector_plugin={blk_sec.get('active_sector_plugin')} ({blk_squad['elapsed_s']}s)"
        )
        out_payload["blk_post_gs"] = {
            "analyze_status": blk_analyze.status_code,
            "squad_elapsed_s": blk_squad["elapsed_s"],
            "is_gated": blk_sec.get("is_gated"),
            "active_sector_plugin": blk_sec.get("active_sector_plugin"),
        }
        if on_checkpoint is not None:
            on_checkpoint(out_payload)

    return out_payload


def run_gate_5_direction_b_watch(
    client: httpx.Client,
    headers: dict[str, str],
) -> dict[str, Any]:
    _log("\n=== Gate 5: Direction B Bank Relative Watch (peer_cet1_buffer_bps) ===")
    resp = _req(
        client,
        "POST",
        "/v2/research/peers/watch",
        headers=headers,
        json_body={
            "primary_symbol": "BAC",
            "symbols": ["BAC", "JPM"],
            "metric_key": "peer_cet1_buffer_bps",
            "operator": ">=",
            "threshold_decimal": "100",
            "description": "V4a live eval BAC CET1 buffer watch",
        },
        timeout=60.0,
    )
    _assert(
        resp.status_code in (200, 201),
        f"Direction B bank watch failed: {resp.status_code} {resp.text[:400]}",
    )
    sub = resp.json()
    triggers = (sub.get("peer_watch_config") or {}).get("triggers") or []
    _assert(
        triggers and triggers[0].get("metric_key") == "peer_cet1_buffer_bps",
        f"Unexpected peer_watch_config triggers: {sub.get('peer_watch_config')}",
    )
    _log(f"  [PASS] Bank Watch created: subscription_id={sub.get('subscription_id')} metric={triggers[0].get('metric_key')}")
    return {
        "subscription_id": sub.get("subscription_id"),
        "status": sub.get("status"),
        "metric_key": triggers[0].get("metric_key"),
    }


def run_gate_6_concurrency_and_memory(
    base_url: str,
    headers: dict[str, str],
    *,
    check_cgroup: bool,
) -> dict[str, Any]:
    _log("\n=== Gate 6: 4-Way Concurrent /v2/research/banks/analyze & Memory Peak Check ===")
    targets = ["JPM", "BAC", "WFC", "C"]

    def _worker(sym: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        with httpx.Client(base_url=base_url, timeout=180.0) as local_client:
            r = local_client.post(
                "/v2/research/banks/analyze",
                headers=headers,
                json={"symbol": sym, "force_refresh": True},
            )
            return {
                "symbol": sym,
                "status_code": r.status_code,
                "elapsed_s": round(time.perf_counter() - t0, 3),
                "cache_hit": r.json().get("cache_hit") if r.status_code == 200 else None,
            }

    t_all = time.perf_counter()
    outcomes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_worker, sym) for sym in targets]
        for fut in as_completed(futs):
            outcomes.append(fut.result())
    wall_s = round(time.perf_counter() - t_all, 3)
    for out in outcomes:
        _assert(out["status_code"] == 200, f"Concurrent bank analyze failed for {out['symbol']}: {out}")

    mem = read_container_memory() if check_cgroup else {}
    if "memory_peak_mib" in mem:
        _assert(
            mem["memory_peak_mib"] < 550.0,
            f"Container memory.peak exceeded 550 MiB ceiling: {mem}",
        )
    _log(f"  [PASS] 4-way concurrent {targets} all HTTP 200 in {wall_s}s; cgroup={mem}")
    return {"wall_s": wall_s, "outcomes": outcomes, "container_memory": mem}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run V4a US Major Bank Live Evaluation")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Target API base URL")
    parser.add_argument("--output-json", default="", help="Optional path to save full JSON summary")
    parser.add_argument("--skip-cgroup", action="store_true", help="Skip docker exec cgroup memory check")
    parser.add_argument("--squad-concurrency", type=int, default=2, help="Bounded parallel Squad workers in Gate 3")
    parser.add_argument("--resume", action="store_true", help="Resume from existing --output-json checkpoint")
    args = parser.parse_args()

    prior_gates: dict[str, Any] = {}
    if args.resume and args.output_json and Path(args.output_json).exists():
        try:
            loaded = json.loads(Path(args.output_json).read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("gates"), dict):
                prior_gates = loaded["gates"]
                _log(f"Resuming from checkpoint {args.output_json} (prior gates={list(prior_gates.keys())})")
        except Exception as exc:
            _log(f"Warning: could not parse resume file {args.output_json}: {exc}")

    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "status": "IN_PROGRESS",
        "gates": dict(prior_gates),
    }

    with httpx.Client(base_url=args.base_url, timeout=300.0) as client:
        health = _req(client, "GET", "/health", timeout=15.0)
        _assert(health.status_code == 200, f"/health check failed: {health.status_code}")

        account_id, headers, email = register_ephemeral_account(client)
        summary["ephemeral_account_id"] = account_id
        summary["ephemeral_email"] = email
        _log(f"Registered ephemeral local account {account_id} ({email})")

        try:
            g1 = run_gate_1_14_issuer_probe(client, headers)
            summary["gates"]["gate1_14_issuer"] = {
                "qualified": g1["qualified"],
                "negative_controls": g1["negative_controls"],
                "coverage": g1["coverage"],
            }
            _save_summary(args.output_json, summary)
            missing = {sym: [field for field in ("tbvps", "rotce", "market_valuation", "sensitivity") if not status[field]]
                       for sym, status in g1["coverage"].items()}
            missing = {sym: fields for sym, fields in missing.items() if fields}
            _assert(not missing, f"Gate 1 coverage incomplete (safety checks passed): {missing}")
            _assert(g1["coverage"]["STT"]["reference_status"] == "matched", "Gate 1 STT reference unavailable")

            prior_g2 = prior_gates.get("gate2_v2_peer_compare")
            if args.resume and isinstance(prior_g2, dict) and prior_g2.get("brief_cited_metrics_count"):
                g2 = prior_g2
                _log(f"\n=== Gate 2: [SKIP/CACHED] Live Brief ({g2.get('brief_cited_metrics_count')} citations) ===")
            else:
                g2 = run_gate_2_v2_peer_compare(client, headers)
                summary["gates"]["gate2_v2_peer_compare"] = g2
                _save_summary(args.output_json, summary)

            def _ckpt_g3_4(g34_payload: dict[str, Any]) -> None:
                summary["gates"]["gate3_and_4_squad_v3a_blk"] = g34_payload
                _save_summary(args.output_json, summary)

            g3_4 = run_gate_3_and_4_squad_and_v3a(
                client,
                headers,
                g1["segments"],
                check_cgroup=not args.skip_cgroup,
                squad_concurrency=max(1, args.squad_concurrency),
                prior_results=prior_gates.get("gate3_and_4_squad_v3a_blk"),
                on_checkpoint=_ckpt_g3_4,
            )
            summary["gates"]["gate3_and_4_squad_v3a_blk"] = g3_4
            _save_summary(args.output_json, summary)

            prior_g5 = prior_gates.get("gate5_direction_b")
            if args.resume and isinstance(prior_g5, dict) and prior_g5.get("metric_key") == "peer_cet1_buffer_bps":
                g5 = prior_g5
                _log(f"\n=== Gate 5: [SKIP/CACHED] Bank Watch ({g5.get('subscription_id')}) ===")
            else:
                g5 = run_gate_5_direction_b_watch(client, headers)
                summary["gates"]["gate5_direction_b"] = g5
                _save_summary(args.output_json, summary)

            g6 = run_gate_6_concurrency_and_memory(
                args.base_url, headers, check_cgroup=not args.skip_cgroup
            )
            summary["gates"]["gate6_concurrency"] = g6
            summary["status"] = "ALL_GATES_PASSED"
        except EvalFailure as exc:
            summary["status"] = "FAILED"
            summary["failure"] = str(exc)
            raise
        finally:
            cleanup = delete_ephemeral_account(client, headers)
            summary["account_cleanup"] = cleanup
            _log(f"\nCleaned up ephemeral account {account_id}: {cleanup}")
            _save_summary(args.output_json, summary)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args.output_json, summary)
    if args.output_json:
        _log(f"Saved report to {args.output_json}")

    _log("\nALL V4A D8 LIVE GATES PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
