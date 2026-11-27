"""Versioned corrections for bank research prepared before cell-level verification."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from typing import Mapping

from .research_bank_versions import BANK_DISCLOSURE_VERSION, BANK_VALUATION_VERSION
from .fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics
from .fin_data.adapters.sec_deep import DualPeriodEarningsRelease
from .research_earnings_squad import EarningsDossier, SquadGap


CORRECTION_CODE = "bank_disclosure_restatement"


def _replay_saved_supplement(snap: Mapping, bank: Mapping) -> dict:
    """Use only the stored filing for this entity and period. No network or model calls."""
    release = snap.get("earnings_release")
    if not release or not bank.get("period_end_date"):
        return {}
    try:
        er = DualPeriodEarningsRelease.model_validate(release)
        end = date.fromisoformat(bank["period_end_date"])
        cutoff = datetime.fromisoformat(str(snap["as_of"])) if snap.get("as_of") else None
    except (TypeError, ValueError):
        return {}
    if (not er.current or er.cik.lstrip("0") != str(bank.get("cik") or "").lstrip("0")
            or snap.get("cik") and er.cik.lstrip("0") != str(snap["cik"]).lstrip("0")):
        return {}
    if not er.current.accession or er.current.accession != bank.get("earnings_8k_accession"):
        return {}
    if not er.cik.isdecimal():
        return {}
    if cutoff:
        published = er.current.accepted_at
        if published is None and cutoff.date() <= er.current.filed_at:
            return {}
        if published and published.replace(tzinfo=published.tzinfo or timezone.utc) > cutoff.replace(tzinfo=cutoff.tzinfo or timezone.utc):
            return {}
    metrics = _extract_8k_supplement_metrics(er.current, end)
    prefix = f"https://www.sec.gov/Archives/edgar/data/{int(er.cik)}/{er.current.accession.replace('-', '')}/"
    verified = {}
    for key, value in metrics.items():
        evidence = metrics.get(key + "_provenance")
        if isinstance(evidence, Mapping) and str(evidence.get("url") or "").startswith(prefix):
            verified[key] = (value, evidence)
    return verified


def _saved_values(bank: Mapping) -> dict:
    bridge = bank.get("tbvps_bridge") or {}
    return {
        "tbvps_usd": bridge.get("tbvps_reported_usd"), "bvps_usd": bridge.get("bvps_reported_usd"),
        "rotce_q_pct": bank.get("rotce_q_reported_pct"), "efficiency_pct": bank.get("efficiency_ratio_reported_pct"),
        "nim_fte_pct": bank.get("nim_fte_pct"), "cet1_std_pct": bank.get("cet1_preliminary_8k_pct"),
    }


def _same_published_value(saved, verified: Decimal) -> bool:
    try:
        return Decimal(str(saved)) == verified.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (ValueError, InvalidOperation):
        return False


def _rechecked_disclosure(snapshot: Mapping) -> dict | None:
    bank = snapshot.get("bank_disclosure") or {}
    if bank.get("schema_version") != "v4a.2":
        return None
    verified = _replay_saved_supplement(snapshot, bank)
    provenance = {key: evidence for key, (_, evidence) in verified.items()}

    def amount(key: str) -> str | None:
        if key in verified:
            return str(verified[key][0].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        return None

    bridge = {**bank["tbvps_bridge"], "tbvps_reported_usd": amount("tbvps_usd"),
              "tbvps_reported_verified": "tbvps_usd" in verified,
              "tbvps_reported_source": "8k_supplement" if "tbvps_usd" in verified else "unavailable",
              "tbvps_reported_provenance": provenance.get("tbvps_usd", {}),
              "tbvps_reported_verification_reason": "archived_cell_verified" if "tbvps_usd" in verified else "missing_verified_cell",
              "bvps_reported_usd": amount("bvps_usd"), "tbvps_gap_pct": None}
    updated = {
        **bank, "schema_version": BANK_DISCLOSURE_VERSION, "tbvps_bridge": bridge,
        "rotce_q_reported_pct": amount("rotce_q_pct"), "rotce_q_verified": "rotce_q_pct" in verified,
        "rotce_q_verification_reason": "archived_cell_verified" if "rotce_q_pct" in verified else "missing_verified_cell",
        "rotce_q_provenance": provenance.get("rotce_q_pct", {}),
        "efficiency_ratio_reported_pct": amount("efficiency_pct"), "nim_fte_pct": amount("nim_fte_pct"),
        "cet1_preliminary_8k_pct": amount("cet1_std_pct"), "metric_provenance": provenance,
        "earnings_8k_exhibits_used": list(dict.fromkeys(p["exhibit"] for p in provenance.values())),
    }
    if bank.get("cet1_standardized_source") == "8k_preliminary":
        updated["cet1_standardized_pct"] = amount("cet1_std_pct")
        if "cet1_std_pct" not in verified:
            updated["cet1_standardized_source"] = None
    return updated


def needs_bank_restatement(snapshot: Mapping) -> bool:
    bank = snapshot.get("bank_disclosure") or {}
    valuation = snapshot.get("bank_valuation") or {}
    if bank.get("schema_version") == "v4a.2" and (
        not valuation or valuation.get("basis_validation_version") == "v1"
    ):
        # The change from .2 to .3 concerns the stored supplement cells only.
        # An independent standardised basis remains valid for this frozen edition.
        used = {k: v for k, v in _saved_values(bank).items() if v is not None}
        if not used:
            return False
        verified = _replay_saved_supplement(snapshot, bank)
        return any(k not in verified or not _same_published_value(v, verified[k][0]) for k, v in used.items())
    return bool(bank and bank.get("schema_version") != BANK_DISCLOSURE_VERSION
                or valuation and valuation.get("basis_validation_version") != BANK_VALUATION_VERSION)


def bank_restatement_notes(raw: Mapping) -> list[str]:
    snap = raw["snapshot"]
    bank = snap.get("bank_disclosure") or {}
    verified = _replay_saved_supplement(snap, bank)
    used = _saved_values(bank)
    mismatched = any(v is not None and k in verified and not _same_published_value(v, verified[k][0])
                     for k, v in used.items())
    if mismatched:
        notes = ["原件复核发现已采用的披露值有误，原估值和依赖这些数字的判断已撤回。"]
    else:
        notes = ["旧版披露未能完整复核期间和单位。相关估值及判断暂缓展示，已核实的披露值列在下方。"
                 if verified else "现有资料不足以复核旧版披露的期间和单位，相关估值及判断暂缓展示。"]
    for key, label, unit in (("tbvps_usd", "每股有形账面价值", "美元"), ("rotce_q_pct", "有形普通股权益回报率", "%")):
        if key not in verified:
            continue
        value, evidence = verified[key]
        notes.append(f"{bank['period_end_date']} 的{label}为 {value:.2f}{unit}。依据为 {evidence['exhibit']} 的"
                     f"“{evidence['row']}”行、“{evidence['column']}”列。[SEC 原始附件]({evidence['url']})")
    return notes


def restated_dossier(raw: Mapping, notes: list[str]) -> EarningsDossier:
    """Serve an independently identifiable correction; leave the original immutable."""
    original_id = raw["dossier_id"]
    revision = sha256("\n".join([raw["dossier_sha256"], BANK_DISCLOSURE_VERSION, *notes]).encode()).hexdigest()[:20]
    gap = SquadGap(gap_id=f"bank_correction_{revision}", stage="disclosure_review",
                   code=CORRECTION_CODE, source=original_id, message=notes[0])
    corrected = dict(raw)
    # Free prose and downstream cards in old versions lack field-level dependencies.
    # Keep their original edition accessible in the archive, not in the current edition.
    snapshot = raw["snapshot"]
    source_refs = [ref for ref in snapshot.get("prefetched_sources", [])
                   if not ref["source_id"].startswith("src_bank_v4a_")]
    corrected.update(
        dossier_id=f"restated_{revision}", dossier_sha256="", supersedes_sha256=raw["dossier_sha256"],
        deep_report_markdown=None,
        dispatch_plan=None, evidence_cards=[], bear_cases=[], rejected_bear_cases=[],
        open_gaps=[gap.model_dump()], memo_markdown="## 银行披露更正\n\n" + "\n\n".join(notes),
        gate_report={**raw["gate_report"], "passed": False, "verified_card_ids": [], "verified_bear_ids": []},
        snapshot={**snapshot, "snapshot_sha256": "", "prefetched_sources": source_refs, "bank_disclosure": _rechecked_disclosure(snapshot),
                  "bank_diagnostics": None, "bank_valuation": None},
    )
    return EarningsDossier.model_validate(corrected)
