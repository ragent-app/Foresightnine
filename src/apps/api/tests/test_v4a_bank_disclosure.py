"""Offline unit & Golden Ground Truth tests for V4a Three-Source Bank Disclosure & Segment Extractor."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from typing import Any

from app.fin_data.adapters.sec_bank_disclosure import (
    BankDisclosureSnapshot,
    extract_bank_segment_snapshot,
    fetch_bank_disclosure_snapshot,
    parse_htm_securities_details,
    parse_r2_income_statement_bank,
    parse_r4_balance_sheet_bank,
    parse_reg_capital_details_table,
    qualify_bank_sector_applicability,
    scan_10q_mda_bank_disclosures_from_text,
)
from app.fin_data.adapters.sec_deep import parse_r_html_table
from app.fin_data.adapters.sec_events.pacing import SecPacingLimiter
from app.research_peer_compare import PeerCompanySnapshot, project_companyfacts_to_snapshot
from app.storage import InMemoryDocumentRepository


def _make_snap(
    *,
    symbol: str,
    cik: str,
    sic: str,
    facts_by_line: dict[str, Any] | None = None,
    dei_shares: list[dict[str, Any]] | None = None,
    latest_acc: str = "0001000000-26-000100",
    latest_filed: str = "2026-11-01",
    pending_acc: str | None = None,
) -> PeerCompanySnapshot:
    return PeerCompanySnapshot(
        cik=cik,
        symbol=symbol,
        company_name=f"{symbol} Corp",
        sic=sic,
        sic_description="Bank / Finance",
        latest_periodic_accession=latest_acc,
        latest_periodic_filed_at=latest_filed,
        xbrl_indexing_pending_accession=pending_acc,
        projection_version=6,
        facts_by_line=facts_by_line or {},
        dei_shares_outstanding=dei_shares or [],
    )


def _inst(val: int | float, end: str = "2026-09-30", filed: str = "2026-11-01", accn: str = "acc-3q26") -> dict[str, Any]:
    return {"end": end, "val": val, "filed": filed, "form": "10-Q", "accn": accn}


def _flow(val: int | float, start: str = "2026-07-01", end: str = "2026-09-30", filed: str = "2026-11-01", accn: str = "acc-3q26", fy: int = 2026, fp: str = "Q3") -> dict[str, Any]:
    return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-Q", "accn": accn, "fy": fy, "fp": fp}


def test_v4a2_qualification_gate_3_subtypes_and_5_negative_controls() -> None:
    """Verify generic 3-subtype bank qualification and strict rejection of BLK, AXP, PGR, PLD, NVDA."""
    # 1. Commercial bank (JPM/BAC/USB): Loans/Assets=40%, noninterest=45%
    comm = _make_snap(
        symbol="JPM", cik="0000019617", sic="6021",
        facts_by_line={
            "total_assets": {"Assets": {"USD": [_inst(4_000_000_000_000)]}},
            "deposits": {"Deposits": {"USD": [_inst(2_400_000_000_000)]}},
            "loans_gross": {"Loans": {"USD": [_inst(1_600_000_000_000)]}},
            "bank_net_revenue": {"RevenuesNetOfInterestExpense": {"USD": [_flow(50_000_000_000)]}},
            "net_interest_income": {"InterestIncomeExpenseNet": {"USD": [_flow(27_500_000_000)]}},
        },
    )
    q_comm = qualify_bank_sector_applicability(sic="6021", snap=comm)
    assert q_comm.is_bank_v4a is True and q_comm.bank_subtype == "universal_commercial"
    assert q_comm.active_sector_plugin == "us_large_bank_v4a"

    # 2. Investment Bank BHC (GS/MS, SIC 6211): Deposits/Assets=27%, Loans/Assets=13%, nonint=80%
    ib_bhc = _make_snap(
        symbol="GS", cik="0000886982", sic="6211",
        facts_by_line={
            "total_assets": {"Assets": {"USD": [_inst(1_700_000_000_000)]}},
            "deposits": {"Deposits": {"USD": [_inst(460_000_000_000)]}},
            "loans_gross": {"Loans": {"USD": [_inst(220_000_000_000)]}},
            "bank_net_revenue": {"RevenuesNetOfInterestExpense": {"USD": [_flow(18_679_000_000)]}},
            "net_interest_income": {"InterestIncomeExpenseNet": {"USD": [_flow(3_700_000_000)]}},
        },
    )
    q_ib = qualify_bank_sector_applicability(sic="6211", snap=ib_bhc)
    assert q_ib.is_bank_v4a is True and q_ib.bank_subtype == "investment_bank_bhc"

    # 3. Custodian / Broker Bank (BK/STT, SIC 6022): Loans/Assets=15%
    cust = _make_snap(
        symbol="BK", cik="0001390777", sic="6022",
        facts_by_line={
            "total_assets": {"Assets": {"USD": [_inst(400_000_000_000)]}},
            "deposits": {"Deposits": {"USD": [_inst(280_000_000_000)]}},
            "loans_gross": {"Loans": {"USD": [_inst(60_000_000_000)]}},
            "bank_net_revenue": {"RevenuesNetOfInterestExpense": {"USD": [_flow(4_600_000_000)]}},
            "net_interest_income": {"InterestIncomeExpenseNet": {"USD": [_flow(1_100_000_000)]}},
        },
    )
    q_cust = qualify_bank_sector_applicability(sic="6022", snap=cust)
    assert q_cust.is_bank_v4a is True and q_cust.bank_subtype == "custodian_or_broker_bank"

    # 4. Five negative controls MUST be rejected cleanly!
    # BLK (SIC 6211 without deposits/loans banking book)
    blk = _make_snap(
        symbol="BLK", cik="0002012383", sic="6211",
        facts_by_line={
            "total_assets": {"Assets": {"USD": [_inst(130_000_000_000)]}},
            "bank_net_revenue": {"Revenues": {"USD": [_flow(5_200_000_000)]}},
        },
    )
    assert qualify_bank_sector_applicability(sic="6211", snap=blk).is_bank_v4a is False
    # AXP (SIC 6199 card issuer EVEN WITH deposits & loans!)
    axp = _make_snap(
        symbol="AXP", cik="0000004962", sic="6199",
        facts_by_line={
            "total_assets": {"Assets": {"USD": [_inst(280_000_000_000)]}},
            "deposits": {"Deposits": {"USD": [_inst(155_000_000_000)]}},
            "loans_gross": {"Loans": {"USD": [_inst(63_900_000_000)]}},
        },
    )
    assert qualify_bank_sector_applicability(sic="6199", snap=axp).is_bank_v4a is False
    assert qualify_bank_sector_applicability(sic="6331", snap=_make_snap(symbol="PGR", cik="0000080661", sic="6331")).is_bank_v4a is False
    assert qualify_bank_sector_applicability(sic="6798", snap=_make_snap(symbol="PLD", cik="0001045609", sic="6798")).is_bank_v4a is False
    assert qualify_bank_sector_applicability(sic="3674", snap=_make_snap(symbol="NVDA", cik="0001045810", sic="3674")).is_bank_v4a is False


def test_v4a2_all_6_major_banks_details_segments_zero_delta_and_traps() -> None:
    """Verify R3-R7 segment parsing achieves exact zero-delta reconciliation across JPM/BAC/WFC/C/GS/MS quirks."""
    cases = [
        # JPM R166: td.fn, sparse Jun 30 column, VISA counterparty block, fragment block, Managed OpSeg + Corp + Reconciling = 50,172 GAAP
        (
            "JPM", "0000019617", "R166.htm",
            """<html><body><table class="report">
              <tr><th class="tl" rowspan="2">Segment Results ($ in Millions)</th><th class="th" colspan="2">3 Months Ended Sep. 30, 2026</th><th class="th" colspan="2">3 Months Ended Jun. 30, 2026</th><th class="th" colspan="2">3 Months Ended Sep. 30, 2025</th></tr>
              <tr><th class="th" colspan="2">Total</th><th class="th" colspan="2">Visa</th><th class="th" colspan="2">Total</th></tr>
              <tr class="re"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_RevenuesNetOfInterestExpense',window);">Total net revenue</a></td><td class="nump">50,172</td><td class="fn">[1]</td><td class="nump"></td><td class="fn"></td><td class="nump">46,427</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_NoninterestExpense',window);">Total noninterest expense</a></td><td class="nump">27,295</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">24,200</td><td class="fn"></td></tr>
              <tr class="re"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_NetIncomeLoss',window);">Net income</a></td><td class="nump">15,305</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">14,000</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl"><a onclick="Show.showAR(this,'defref_srt_CounterpartyNameAxis=jpm_VISAMember',window);">VISA Inc.</a></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
              <tr class="ro"><td class="pl">Net gain</td><td></td><td></td><td class="nump">4,600</td><td></td><td></td><td></td></tr>
              <tr class="rh"><td class="pl"><a onclick="Show.showAR(this,'defref_srt_ConsolidationItemsAxis=us-gaap_OperatingSegmentsMember',window);">Operating Segments | Consumer &amp; Community Banking [Member]</a></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
              <tr class="re"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_RevenuesNetOfInterestExpense',window);">Total net revenue</a></td><td class="nump">21,171</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">19,500</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_NetIncomeLoss',window);">Net income</a></td><td class="nump">5,800</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">5,100</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl"><a onclick="Show.showAR(this,'defref_srt_ConsolidationItemsAxis=us-gaap_OperatingSegmentsMember',window);">Operating Segments | Commercial &amp; Investment Bank [Member]</a></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">22,734</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">20,000</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">7,100</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">6,200</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl"><a onclick="Show.showAR(this,'defref_srt_ConsolidationItemsAxis=us-gaap_OperatingSegmentsMember',window);">Operating Segments | Asset &amp; Wealth Management [Member]</a></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">6,909</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">6,100</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">1,900</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">1,500</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl"><a onclick="Show.showAR(this,'defref_srt_ConsolidationItemsAxis=us-gaap_OperatingSegmentsMember',window);">Corporate</a></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">93</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">120</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">505</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">400</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl"><a onclick="Show.showAR(this,'defref_srt_ConsolidationItemsAxis=us-gaap_MaterialReconcilingItemsMember',window);">Reconciling Items</a></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">(735)</td><td class="fn">[1]</td><td class="nump"></td><td class="fn"></td><td class="nump">(550)</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">0</td><td class="fn"></td><td class="nump"></td><td class="fn"></td><td class="nump">0</td><td class="fn"></td></tr>
              <tr><td colspan="7"><table class="outerFootnotes"><tr class="outerFootnote"><td>[1] Segment results are on a managed basis (FTE).</td></tr></table></td></tr>
            </table></body></html>""",
            "50172000000", "gaap", "managed_fte", "735000000", 3,
        ),
        # BAC R113: Block 0 is FTE (30,119) with FullTaxEquivalentBasisAfterTax (161M) -> GAAP 29,958!
        (
            "BAC", "0000070858", "R113.htm",
            """<html><body><table class="report">
              <tr><th class="tl">Segment Information ($ in Millions)</th><th class="th">3 Months Ended Sep. 30, 2026</th></tr>
              <tr class="re"><td class="pl"><a onclick="Show.showAR(this,'defref_bac_RevenuesNetOfInterestExpenseFullTaxEquivalentBasis',window);">Total revenue, net of interest expense (FTE)</a></td><td class="nump">30,119</td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_bac_InterestIncomeExpenseNetFullTaxEquivalentBasis',window);">Net interest income (FTE)</a></td><td class="nump">16,610</td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_NoninterestExpense',window);">Noninterest expense</a></td><td class="nump">19,722</td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_NetIncomeLoss',window);">Net income</a></td><td class="nump">6,933</td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_bac_InterestIncomeExpenseNetFullTaxEquivalentBasisAfterTax',window);">FTE adjustment</a></td><td class="nump">161</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Consumer Banking [Member]</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">11,572</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">3,200</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Global Wealth &amp; Investment Management [Member]</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">7,118</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">1,400</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Global Banking [Member]</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">6,205</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">1,900</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Global Markets [Member]</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">5,867</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">1,500</td></tr>
              <tr class="rh"><td class="pl">All Other</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">(643)</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">(1,067)</td></tr>
            </table></body></html>""",
            "30119000000", "fte", "managed_fte", "161000000", 4,
        ),
        # WFC R121: NetIncomeLoss (1,195) strictly prioritized over ContinuingOpsIncludingNoncontrolling (1,196)!
        (
            "WFC", "0000072971", "R121.htm",
            """<html><body><table class="report">
              <tr><th class="tl">Segment Information ($ in Millions)</th><th class="th" colspan="2">3 Months Ended Sep. 30, 2026</th></tr>
              <tr class="re"><td class="pl">Total revenue</td><td class="nump">21,870</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Noninterest expense</td><td class="nump">13,743</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Provision for credit losses</td><td class="nump">1,029</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Income before tax</td><td class="nump">7,098</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">5,951</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl">Consumer Banking and Lending [Member]</td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total revenue</td><td class="nump">10,656</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">2,200</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl">Commercial Banking [Member]</td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total revenue</td><td class="nump">3,236</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest',window);">Segment profit before NCI</a></td><td class="nump">1,196</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_NetIncomeLoss',window);">Net income</a></td><td class="nump">1,195</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl">Corporate and Investment Banking [Member]</td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total revenue</td><td class="nump">5,008</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">1,800</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl">Wealth and Investment Management [Member]</td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total revenue</td><td class="nump">4,104</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">700</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl">Corporate</td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total revenue</td><td class="nump">(610)</td><td class="fn"></td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">56</td><td class="fn"></td></tr>
              <tr class="rh"><td class="pl">Reconciling Items</td><td></td><td></td></tr>
              <tr class="re"><td class="pl">Total revenue</td><td class="nump">(524)</td><td class="fn">[1]</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">0</td><td class="fn"></td></tr>
            </table></body></html>""",
            "21870000000", "gaap", "managed_fte", "524000000", 4,
        ),
        # C R61: Operating Segments subtotal + 5 segments (22,401) + All Other (1,432) + Reconciling (+12) = 23,845!
        (
            "C", "0000831001", "R61.htm",
            """<html><body><table class="report">
              <tr><th class="tl">Segment Information ($ in Millions)</th><th class="th">3 Months Ended Sep. 30, 2026</th></tr>
              <tr class="re"><td class="pl">Total revenues, net of interest expense</td><td class="nump">23,845</td></tr>
              <tr class="ro"><td class="pl">Total operating expenses</td><td class="nump">13,952</td></tr>
              <tr class="ro"><td class="pl">Provision for credit losses</td><td class="nump">2,586</td></tr>
              <tr class="ro"><td class="pl">Income before tax</td><td class="nump">7,307</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">5,245</td></tr>
              <tr class="rh"><td class="pl">Operating Segments</td><td></td></tr>
              <tr class="re"><td class="pl">Total revenues, net of interest expense</td><td class="nump">22,401</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Services</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">6,614</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Markets</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">6,113</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Banking</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">1,741</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | Wealth</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">3,269</td></tr>
              <tr class="rh"><td class="pl">Operating Segments | U.S. Personal Banking</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">4,664</td></tr>
              <tr class="rh"><td class="pl">All Other</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">1,432</td></tr>
              <tr class="ro"><td class="pl">Net interest income</td><td class="nump">1,018</td></tr>
              <tr class="rh"><td class="pl">Reconciling Items</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">12</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">0</td></tr>
            </table></body></html>""",
            "23845000000", "gaap", "gaap", None, 5,
        ),
        # GS R175 + MS R127
        (
            "GS", "0000886982", "R175.htm",
            """<html><body><table class="report">
              <tr><th class="tl">Segment Operating Results ($ in Millions)</th><th class="th">3 Months Ended Sep. 30, 2026</th></tr>
              <tr class="re"><td class="pl">Total net revenues</td><td class="nump">18,679</td></tr>
              <tr class="ro"><td class="pl">Operating expenses</td><td class="nump">11,308</td></tr>
              <tr class="ro"><td class="pl">Provision for credit losses</td><td class="nump">368</td></tr>
              <tr class="ro"><td class="pl">Pre-tax earnings</td><td class="nump">7,003</td></tr>
              <tr class="ro"><td class="pl">Net earnings</td><td class="nump">5,294</td></tr>
              <tr class="rh"><td class="pl">Global Banking &amp; Markets</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">13,458</td></tr>
              <tr class="rh"><td class="pl">Asset &amp; Wealth Management</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">5,051</td></tr>
              <tr class="rh"><td class="pl">Platform Solutions</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">170</td></tr>
            </table></body></html>""",
            "18679000000", "gaap", "gaap", None, 3,
        ),
        (
            "MS", "0000895421", "R127.htm",
            """<html><body><table class="report">
              <tr><th class="tl">Segment Information ($ in Millions)</th><th class="th">3 Months Ended Sep. 30, 2026</th></tr>
              <tr class="re"><td class="pl">Total net revenues</td><td class="nump">20,338</td></tr>
              <tr class="ro"><td class="pl">Non-interest expenses</td><td class="nump">13,712</td></tr>
              <tr class="ro"><td class="pl">Provision for credit losses</td><td class="nump">148</td></tr>
              <tr class="ro"><td class="pl">Income before tax</td><td class="nump">6,478</td></tr>
              <tr class="ro"><td class="pl">Net income</td><td class="nump">4,909</td></tr>
              <tr class="rh"><td class="pl">Institutional Securities Segment</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">10,029</td></tr>
              <tr class="rh"><td class="pl">Wealth Management Segment</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">9,065</td></tr>
              <tr class="rh"><td class="pl">Investment Management Segment</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">1,459</td></tr>
              <tr class="rh"><td class="pl">Intersegment Eliminations</td><td></td></tr>
              <tr class="re"><td class="pl">Total net revenue</td><td class="nump">(215)</td></tr>
            </table></body></html>""",
            "20338000000", "gaap", "gaap", None, 3,
        ),
    ]

    for sym, cik, r_file, html, exp_cons, exp_c_basis, exp_s_basis, exp_fte, exp_n_seg in cases:
        tbl = parse_r_html_table(html, r_file=r_file, url=f"https://www.sec.gov/{r_file}")
        seg = extract_bank_segment_snapshot(tbl, cik=cik, symbol=sym, accession=f"acc-{sym}", source_url=f"https://www.sec.gov/{r_file}")
        assert seg is not None, sym
        assert seg.consolidated_net_revenue_usd == exp_cons, sym
        assert seg.consolidated_basis == exp_c_basis, sym
        assert seg.segments_basis == exp_s_basis, sym
        assert seg.fte_reconciling_adjustment_usd == exp_fte, sym
        assert len(seg.operating_segments) == exp_n_seg, sym
        assert seg.reconciliation_delta_usd == "0" and seg.reconciliation_status == "exact", sym
        if sym == "WFC":
            cb_row = next(r for r in seg.operating_segments if "Commercial Banking" in r.display_name)
            assert cb_row.net_income_usd == "1195000000"


def test_v4a2_r4_vie_guard_r8_bhc_filter_citigroup_ttm_and_gs_10q_rsu_bridge() -> None:
    """Verify R4 VIE stop, R8 sub-bank exclusion + R4b ratio norm, Citigroup 9M+FY TTM, and GS 10-Q RSU bridge."""
    # 1. R4 Balance Sheet VIE guard + R8 BHC filter + HTM fair value
    r4_html = """<html><body><table class="report">
      <tr><th class="tl">Balance Sheet ($ in Millions)</th><th class="th">Sep. 30, 2026</th></tr>
      <tr class="ro"><td class="pl">Loans</td><td class="nump">$ 1,558,369</td></tr>
      <tr class="ro"><td class="pl">Allowance for loan losses</td><td class="nump">(13,005)</td></tr>
      <tr class="ro"><td class="pl">Deposits</td><td class="nump">2,400,000</td></tr>
      <tr class="ro"><td class="pl">Preferred stock</td><td class="nump">24,996</td></tr>
      <tr class="ro"><td class="pl">Total stockholders' equity</td><td class="nump">296,749</td></tr>
      <tr class="ro"><td class="pl">Goodwill</td><td class="nump">69,021</td></tr>
      <tr class="ro"><td class="pl">Intangible assets</td><td class="nump">1,784</td></tr>
      <tr class="ro"><td class="pl">Total assets</td><td class="nump">3,300,000</td></tr>
      <tr class="ro"><td class="pl">Total liabilities and stockholders' equity</td><td class="nump">3,300,000</td></tr>
      <tr class="rh"><td class="pl">Consolidated Variable Interest Entities (VIEs)</td><td></td></tr>
      <tr class="ro"><td class="pl">Loans</td><td class="nump">33,791</td></tr>
      <tr class="ro"><td class="pl">Allowance for loan losses</td><td class="nump">(849)</td></tr>
    </table></body></html>"""
    r4_bs = parse_r4_balance_sheet_bank(parse_r_html_table(r4_html, r_file="R4.htm", url="https://www.sec.gov/R4.htm"))
    assert r4_bs["loans_gross"] == Decimal("1558369000000")  # NOT overwritten by 33,791 VIE!
    assert r4_bs["acl"] == Decimal("13005000000")             # NOT overwritten by 849 VIE!
    assert r4_bs["preferred_stock"] == Decimal("24996000000")

    rc_html = """<html><body><table class="report">
      <tr><th class="tl">Regulatory Capital ($ in Millions)</th><th class="th">Sep. 30, 2026</th></tr>
      <tr class="rh"><td class="pl">JPMorgan Chase Bank, N.A. [Member]</td><td></td></tr>
      <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_CommonEquityTierOneCapitalRatio',window);">CET1 ratio</a></td><td class="nump">0.152</td></tr>
      <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_CommonEquityTierOneCapital',window);">CET1 capital</a></td><td class="nump">296,411</td></tr>
      <tr class="rh"><td class="pl">JPMorgan Chase &amp; Co. Consolidated (BHC)</td><td></td></tr>
      <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_CommonEquityTierOneCapitalRatio',window);">CET1 ratio</a></td><td class="nump">0.140</td></tr>
      <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_CommonEquityTierOneCapital',window);">CET1 capital</a></td><td class="nump">304,146</td></tr>
      <tr class="ro"><td class="pl">Stress capital buffer</td><td class="nump">0.025</td></tr>
      <tr class="ro"><td class="pl">G-SIB surcharge</td><td class="nump">4.50%</td></tr>
      <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_RiskWeightedAssets',window);">Risk-weighted assets</a></td><td class="nump">2,172,471</td></tr>
    </table></body></html>"""
    rc = parse_reg_capital_details_table([parse_r_html_table(rc_html, r_file="R156.htm", url="https://www.sec.gov/R156.htm")])
    assert rc["cet1_standardized_pct"] == Decimal("14.000")   # BHC 14.0%, NOT sub-bank 15.2%!
    assert rc["cet1_capital_usd"] == Decimal("304146000000")
    assert rc["cet1_reg_min_pct"] == Decimal("11.500")        # 4.5 + 2.5 (from 0.025!) + 4.50

    htm_html = """<html><body><table class="report">
      <tr><th class="tl">Held-to-Maturity Securities ($ in Millions)</th><th class="th">Sep. 30, 2026</th></tr>
      <tr class="rh"><td class="pl">Held-to-Maturity Securities</td><td></td></tr>
      <tr class="ro"><td class="pl">Amortized cost</td><td class="nump">496,398</td></tr>
      <tr class="ro"><td class="pl">Fair value</td><td class="nump">392,513</td></tr>
    </table></body></html>"""
    htm = parse_htm_securities_details([parse_r_html_table(htm_html, r_file="R55.htm", url="https://www.sec.gov/R55.htm")])
    assert htm["htm_net_unrealized_pnl_usd"] == Decimal("-103885000000")

    # 2. Citigroup full pipeline: stale 2010 shares + 2025-12-31 companyfacts lag -> single R2.htm 9M + FY2025 TTM + R4 shares!
    c_acc_3q26 = "0000831001-26-000056"
    c_cf = {
        "facts": {
            "us-gaap": {
                "RevenuesNetOfInterestExpense": {"units": {"USD": [{"start": "2025-01-01", "end": "2025-12-31", "val": 85_000_000_000, "filed": "2026-02-20", "form": "10-K"}]}},
                "NoninterestExpense": {"units": {"USD": [{"start": "2025-01-01", "end": "2025-12-31", "val": 52_000_000_000, "filed": "2026-02-20", "form": "10-K"}]}},
                "NetIncomeLossAvailableToCommonStockholdersBasic": {"units": {"USD": [{"start": "2025-01-01", "end": "2025-12-31", "val": 14_000_000_000, "filed": "2026-02-20", "form": "10-K"}]}},
                # 2010 pre-split trap (29,049.6M shares)!
                "CommonStockSharesOutstanding": {"units": {"shares": [{"end": "2010-09-30", "val": 29_049_600_000, "filed": "2010-11-05", "form": "10-Q"}]}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {"units": {"shares": [{"end": "2026-10-15", "val": 1_655_200_000, "filed": "2026-11-03", "form": "10-Q"}]}},
            },
        }
    }
    c_snap = project_companyfacts_to_snapshot(
        cik="0000831001", symbol="C", company_name="Citigroup Inc.", sic="6021", sic_description="Commercial Bank",
        latest_periodic_accession=c_acc_3q26, latest_periodic_filed_at="2026-11-03", companyfacts_doc=c_cf,
    )
    c_fs = """<?xml version="1.0"?><FilingSummary><MyReports>
      <Report><HtmlFileName>R2.htm</HtmlFileName><ShortName>Consolidated Statement of Income</ShortName><LongName>Statements of Income</LongName><MenuCategory>Statements</MenuCategory><Position>2</Position></Report>
      <Report><HtmlFileName>R4.htm</HtmlFileName><ShortName>Consolidated Balance Sheet</ShortName><LongName>Consolidated Balance Sheets</LongName><MenuCategory>Statements</MenuCategory><Position>4</Position></Report>
    </MyReports></FilingSummary>"""
    c_r2 = """<html><body><table class="report">
      <tr><th class="tl" rowspan="2">Income Statement ($ in Millions)</th><th class="th" colspan="2">3 Months Ended</th><th class="th" colspan="2">9 Months Ended</th></tr>
      <tr><th class="th">Sep. 30, 2026</th><th class="th">Sep. 30, 2025</th><th class="th">Sep. 30, 2026</th><th class="th">Sep. 30, 2025</th></tr>
      <tr class="re"><td class="pl">Total revenues, net of interest expense</td><td class="nump">23,845</td><td class="nump">20,500</td><td class="nump">73,244</td><td class="nump">65,354</td></tr>
      <tr class="ro"><td class="pl">Net interest income</td><td class="nump">14,500</td><td class="nump">13,200</td><td class="nump">44,000</td><td class="nump">40,000</td></tr>
      <tr class="ro"><td class="pl">Total operating expenses</td><td class="nump">13,952</td><td class="nump">13,100</td><td class="nump">41,500</td><td class="nump">39,500</td></tr>
      <tr class="ro"><td class="pl">Provision for credit losses</td><td class="nump">2,586</td><td class="nump">2,200</td><td class="nump">7,800</td><td class="nump">6,900</td></tr>
      <tr class="ro"><td class="pl">Net income</td><td class="nump">4,892</td><td class="nump">3,800</td><td class="nump">15,000</td><td class="nump">12,000</td></tr>
    </table></body></html>"""
    c_r4 = """<html><body><table class="report">
      <tr><th class="tl">Balance Sheet ($ in Millions)</th><th class="th">Sep. 30, 2026</th></tr>
      <tr class="ro"><td class="pl">Total assets</td><td class="nump">2,450,000</td></tr>
      <tr class="ro"><td class="pl">Deposits</td><td class="nump">1,310,000</td></tr>
      <tr class="ro"><td class="pl">Loans</td><td class="nump">690,000</td></tr>
      <tr class="ro"><td class="pl">Preferred stock</td><td class="nump">18,050</td></tr>
      <tr class="ro"><td class="pl">Total stockholders' equity</td><td class="nump">209,409</td></tr>
      <tr class="ro"><td class="pl">Goodwill</td><td class="nump">19,072</td></tr>
      <tr class="ro"><td class="pl">Intangible assets</td><td class="nump">4,138</td></tr>
    </table></body></html>"""
    c_subs = {
        "cik": "0000831001",
        "filings": {
            "recent": {
                "accessionNumber": [c_acc_3q26, "0001104659-26-115323"],
                "filingDate": ["2026-11-03", "2026-10-14"],
                "reportDate": ["2026-09-30", "2026-09-30"],
                "form": ["10-Q", "8-K"],
                "items": [[], ["2.02", "9.01"]],
                "primaryDocument": ["c-20260930.htm", "c-8k.htm"],
            }
        },
    }
    from types import SimpleNamespace
    def _c_transport(method: str, url: str, headers: dict, body: bytes, timeout: float) -> Any:
        if url.endswith("FilingSummary.xml"):
            return SimpleNamespace(status_code=200, headers={"content-type": "application/xml"}, body=c_fs.encode())
        if url.endswith("R2.htm"):
            return SimpleNamespace(status_code=200, headers={"content-type": "text/html"}, body=c_r2.encode())
        if url.endswith("R4.htm"):
            return SimpleNamespace(status_code=200, headers={"content-type": "text/html"}, body=c_r4.encode())
        if url.endswith("index.json"):
            return SimpleNamespace(status_code=200, headers={"content-type": "application/json"}, body=json.dumps({
                "directory": {"item": [{"name": "c-8k.htm", "type": "8-K"}, {"name": "ex991.htm", "type": "EX-99.1"}]}
            }).encode())
        if url.endswith("ex991.htm"):
            return SimpleNamespace(status_code=200, headers={"content-type": "text/html"}, body=b"<html><body><table><tr><th>Metric</th><th>3Q26</th></tr><tr><td>ROTCE</td><td>11.5%</td></tr><tr><td>Tangible book value per share</td><td>$101.59</td></tr><tr><td>CET1 ratio</td><td>12.8%</td></tr></table></body></html>")
        return SimpleNamespace(status_code=404, headers={}, body=b"")

    bank_repo = InMemoryDocumentRepository()
    disc, hit = fetch_bank_disclosure_snapshot(
        snap=c_snap,
        user_agent="ForesightNine-Test/1.0 test@example.com",
        limiter=SecPacingLimiter(rate_per_sec=9.0, burst=9.0),
        bank_cache=bank_repo,
        submissions=c_subs,
        transport=_c_transport,
        preloaded_10q_text="CET1 Capital ratio requirement 11.6 % including stress capital buffer of 3.6 % and method 2 G-SIB surcharge of 3.5 %.",
    )
    assert hit is False and disc is not None
    assert disc.xbrl_companyfacts_fresh is False
    # TTM = 73,244 + 85,000 - 65,354 = 92,890M USD!
    assert disc.net_revenue_gaap_ttm_usd == "92890000000"
    assert disc.cet1_reg_min_pct == "11.60"
    # A later DEI cover date cannot stand in for period-end shares.
    assert disc.tbvps_bridge.tbvps_xbrl_std_usd is None
    assert disc.tbvps_bridge.tbvps_reported_usd is None
    assert not disc.tbvps_bridge.tbvps_xbrl_std_verified
    assert not disc.tbvps_bridge.tbvps_reported_verified
    assert disc.tbvps_bridge.shares_ending_common is None
    assert disc.tbvps_bridge.rsu_share_addon is None
    assert disc.avg_tangible_common_equity_q_usd is None

    # Old snapshots cannot bypass the current eligibility rules.
    from app.fin_data.adapters.sec_bank_disclosure import BankDisclosureSnapshot
    for old_version in ("v4a.1", "v4a.2"):
        previous = {**disc.model_dump(mode="json"), "schema_version": old_version}
        bank_repo.put("bank:0000831001:latest", previous)
        assert BankDisclosureSnapshot.model_validate(previous).schema_version == old_version
        old, old_hit = fetch_bank_disclosure_snapshot(
            snap=c_snap, user_agent="u", limiter=SecPacingLimiter(rate_per_sec=9.0, burst=9.0),
            bank_cache=bank_repo, cache_only=True,
        )
        assert old is None and not old_hit
    bank_repo.put("bank:0000831001:latest", disc.model_dump(mode="json"))
    # Cache hit under cache_only=True
    disc_cached, hit2 = fetch_bank_disclosure_snapshot(
        snap=c_snap, user_agent="u", limiter=SecPacingLimiter(rate_per_sec=9.0, burst=9.0),
        bank_cache=bank_repo, cache_only=True,
    )
    assert hit2 is True and disc_cached is not None and disc_cached.net_revenue_gaap_ttm_usd == "92890000000"

    # 3. GS 10-Q MD&A scan & BAC 'percent' wording
    gs_mda = scan_10q_mda_bank_disclosures_from_text(
        "Tangible common shareholders' equity $ 100,927 Book value per common share $ 372.64 "
        "Tangible book value per common share $ 341.43 ROTE 20.5 % ROE 18.7 %"
    )
    assert gs_mda["tbvps_reported_usd"] == Decimal("341.43")
    assert gs_mda["rotce_q_reported_pct"] == Decimal("20.5")
    bac_mda = scan_10q_mda_bank_disclosures_from_text(
        "At September 30, 2026, the Corporation's SCB was 2.5 percent, with a Common equity tier 1 (CET1) minimum requirement of 10.0 percent."
    )
    assert bac_mda["cet1_reg_min_pct"] == Decimal("10.0") and bac_mda["cet1_scb_pct"] == Decimal("2.5")


def test_v4a5_reg_capital_rwa_ratio_row_ignored_and_implied_rwa_fallback() -> None:
    """Percentage rows labeled with risk-weighted assets (e.g. '10.5% as a % of RWA') must not overwrite RWA USD."""
    r_pct_trap = """<html><body><table class="report">
      <tr><th class="tl">Regulatory Capital ($ in Millions)</th><th class="th">Sep. 30, 2026</th></tr>
      <tr class="re"><td class="pl">Standardized Approach</td><td class="nump"></td></tr>
      <tr class="ro"><td class="pl">Common Equity Tier 1 capital ratio</td><td class="nump">14.0%</td></tr>
      <tr class="ro"><td class="pl">Common Equity Tier 1 capital</td><td class="nump">$304,146</td></tr>
      <tr class="ro"><td class="pl">Risk-weighted assets as a % of total assets (ratio)</td><td class="nump">10.5%</td></tr>
      <tr class="ro"><td class="pl">Risk-weighted assets</td><td class="nump">$2,172,471</td></tr>
    </table></body></html>"""
    parsed = parse_r_html_table(r_pct_trap, r_file="R35.htm", url="https://www.sec.gov/R35.htm")
    assert parsed is not None
    rc = parse_reg_capital_details_table([parsed])
    assert rc["cet1_rwa"] == Decimal("2172471000000")

    # Even if R8 table only has the ratio-like small row (no valid dollar row), fetch_bank_disclosure_snapshot
    # rejects implausible RWA (< 5% assets) and derives RWA from CET1 capital / CET1 ratio when available.
    r_only_trap = """<html><body><table class="report">
      <tr><th class="tl">Regulatory Capital ($ in Millions)</th><th class="th">Sep. 30, 2026</th></tr>
      <tr class="re"><td class="pl">Standardized Approach</td><td class="nump"></td></tr>
      <tr class="ro"><td class="pl">Common Equity Tier 1 capital ratio</td><td class="nump">14.0%</td></tr>
      <tr class="ro"><td class="pl">Common Equity Tier 1 capital</td><td class="nump">$304,146</td></tr>
    </table></body></html>"""
    snap = _make_snap(
        symbol="JPM",
        cik="0000019617",
        sic="6021",
        facts_by_line={
            "revenue": {"us-gaap:Revenues": {"USD": [{"start": "2026-07-01", "end": "2026-09-30", "val": 45000000000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-01", "accn": "0001000000-26-000100"}]}},
            "net_income": {"us-gaap:NetIncomeLoss": {"USD": [{"start": "2026-07-01", "end": "2026-09-30", "val": 12000000000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-01", "accn": "0001000000-26-000100"}]}},
            "total_assets": {"us-gaap:Assets": {"USD": [{"end": "2026-09-30", "val": 4000000000000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-01", "accn": "0001000000-26-000100"}]}},
            "cet1_capital": {"us-gaap:CommonEquityTierOneCapital": {"USD": [{"end": "2026-09-30", "val": 304146000000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-01", "accn": "0001000000-26-000100"}]}},
            "rwa": {"us-gaap:RiskWeightedAssets": {"USD": [{"end": "2026-09-30", "val": 105000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-01", "accn": "0001000000-26-000100"}]}},
        },
        dei_shares=[{"end": "2026-09-30", "val": 2800000000, "filed": "2026-11-01", "form": "10-Q", "accn": "0001000000-26-000100"}],
    )
    from types import SimpleNamespace
    fs_xml = """<FilingSummary><MyReports><Report><HtmlFileName>R35.htm</HtmlFileName><ShortName>Regulatory Capital</ShortName><LongName>Regulatory Capital Details</LongName><MenuCategory>Details</MenuCategory><Position>35</Position></Report></MyReports></FilingSummary>"""
    def _tr(method: str, url: str, headers: dict, body: bytes, timeout: float) -> Any:
        if url.endswith("FilingSummary.xml"):
            return SimpleNamespace(status_code=200, headers={"content-type": "application/xml"}, body=fs_xml.encode())
        if url.endswith("R35.htm"):
            return SimpleNamespace(status_code=200, headers={"content-type": "text/html"}, body=r_only_trap.encode())
        return SimpleNamespace(status_code=404, headers={}, body=b"")
    disc, _ = fetch_bank_disclosure_snapshot(
        snap=snap,
        user_agent="ForesightNine-Test/1.0 test@example.com",
        limiter=SecPacingLimiter(rate_per_sec=9.0, burst=9.0),
        submissions={"cik": "0000019617", "filings": {"recent": {"accessionNumber": ["0001000000-26-000100"], "filingDate": ["2026-11-01"], "reportDate": ["2026-09-30"], "form": ["10-Q"], "items": [[]], "primaryDocument": ["jpm.htm"]}}},
        transport=_tr,
    )
    assert disc is not None and disc.rwa_standardized_usd is not None
    assert Decimal(disc.rwa_standardized_usd) > Decimal("2000000000000")


def test_period_table_metrics_selects_current_consolidated_cell_and_preserves_provenance() -> None:
    from datetime import date
    from app.fin_data.adapters.sec_deep import EarningsReleaseExhibit, EarningsReleasePeriod, ParsedHtmlTable
    from app.fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics
    table = ParsedHtmlTable(headers=["Consolidated results (USD per share)", "3Q25", "2Q26", "3Q26", "YTD2026"], rows=[
        ["Tangible book value per common share", "55.57", "59.23", "59.69", "59.69"],
        ["Return on average tangible common equity", "20.9%", "25.5%", "25.1%", "22.8%"],
    ])
    toc = ParsedHtmlTable(headers=["Contents", "3Q26"], rows=[["Tangible book value per common share", "43"]])
    exhibit = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x.htm", url="https://sec.test/x.htm", text="", tables=[toc, table], content_sha256="x")
    period = EarningsReleasePeriod(period_role="current", accession="acc", filed_at=date(2026, 10, 15), period_of_report=date(2026, 9, 30), index_json_url="x", ex99_1=exhibit)
    metrics = _extract_8k_supplement_metrics(period, date(2026, 9, 30))
    assert metrics["tbvps_usd"] == Decimal("59.69")
    assert metrics["rotce_q_pct"] == Decimal("25.1")
    assert metrics["tbvps_usd_provenance"]["unit"] == "USD/share"
    assert metrics["rotce_q_pct_provenance"]["column"] == "3Q26"
    assert "tbvps_usd" not in _extract_8k_supplement_metrics(period, date(2026, 6, 30))


def test_period_table_rejects_segment_and_ambiguous_columns() -> None:
    from datetime import date
    from app.fin_data.adapters.sec_deep import EarningsReleaseExhibit, EarningsReleasePeriod, ParsedHtmlTable
    from app.fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics
    segment = ParsedHtmlTable(headers=["Business segment", "3Q26"], rows=[["Return on average tangible common equity", "25.1%"]])
    ambiguous = ParsedHtmlTable(headers=["Consolidated", "3Q26", "3Q26"], rows=[["Tangible book value per share", "59.69", "45.00"]])
    foreign_currency = ParsedHtmlTable(headers=["Consolidated results in CAD", "3Q26"], rows=[
        ["Tangible book value per common share", "$59.69"],
    ])
    exhibit = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x", url="x", text="", tables=[segment, ambiguous, foreign_currency], content_sha256="x")
    period = EarningsReleasePeriod(period_role="current", accession="acc", filed_at=date(2026, 10, 15), period_of_report=date(2026, 9, 30), index_json_url="x", ex99_1=exhibit)
    result = _extract_8k_supplement_metrics(period, date(2026, 9, 30))
    assert "tbvps_usd" not in result
    assert "rotce_q_pct" not in result


def test_dated_table_currency_cell_and_unsafe_metrics():
    from datetime import date
    from app.fin_data.adapters.sec_deep import EarningsReleaseExhibit, EarningsReleasePeriod, ParsedHtmlTable
    from app.fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics
    table = ParsedHtmlTable(headers=["Consolidated capital", "Sept. 30, 2025", "", "June 30, 2025", ""], rows=[
        ["Tangible book value per common share – Non-GAAP (b)", "", "$30.60", "", "$29.57"],
        ["CET1 ratio", "11.7%", "", "11.5%", ""],
        ["Return on average tangible common equity", "25.6%", "", "27.8%", ""],
    ])
    exhibit = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x", url="x", text="", tables=[table], content_sha256="x")
    period = EarningsReleasePeriod(period_role="current", accession="acc", filed_at=date(2025, 10, 16), period_of_report=date(2025, 9, 30), index_json_url="x", ex99_1=exhibit)
    result = _extract_8k_supplement_metrics(period, date(2025, 9, 30))
    assert result["tbvps_usd"] == Decimal("30.60")
    assert "cet1_std_pct" not in result
    # A date alone does not establish a quarterly duration for a flow ratio.
    assert "rotce_q_pct" not in result


def test_quarter_header_and_explicit_units_block_annual_or_ambiguous_ratio():
    from datetime import date
    from app.fin_data.adapters.sec_deep import EarningsReleaseExhibit, EarningsReleasePeriod, ParsedHtmlTable
    from app.fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics
    table = ParsedHtmlTable(headers=["Consolidated results", "", "", ""], rows=[
        ["", "Quarter ended", "", "Year to date"],
        ["", "3Q25", "2Q25", "2025"],
        ["Efficiency ratio", "54%", "56%", "70%"],
        ["Net interest margin (FTE)", "3.2%", "3.1%", "3.0%"],
        ["Standardized CET1 capital ratio", "13.5%", "13.2%", "12%"],
        ["Return on average tangible common equity", "0.25", "0.24", "0.23"],
    ])
    exhibit = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x", url="x", text="", tables=[table], content_sha256="x")
    period = EarningsReleasePeriod(period_role="current", accession="acc", filed_at=date(2025, 10, 16), period_of_report=date(2025, 10, 16), index_json_url="x", ex99_1=exhibit)
    result = _extract_8k_supplement_metrics(period, date(2025, 9, 30))
    assert result["efficiency_pct"] == Decimal("54")
    assert result["nim_fte_pct"] == Decimal("3.2")
    assert result["cet1_std_pct"] == Decimal("13.5")
    assert "rotce_q_pct" not in result
    assert result["nim_fte_pct_provenance"]["unit"] == "percent"


def test_balance_sheet_does_not_invent_absent_preferred_equity():
    html = '''<html><body><table class="report">
      <tr><th class="tl">Balance Sheet ($ in Millions)</th><th class="th">Sep. 30, 2026</th></tr>
      <tr class="ro"><td class="pl">Total assets</td><td class="nump">320,000</td></tr>
      <tr class="ro"><td class="pl">Deposits</td><td class="nump">200,000</td></tr>
      <tr class="ro"><td class="pl">Total stockholders' equity</td><td class="nump">28,710</td></tr>
      <tr class="ro"><td class="pl">Goodwill</td><td class="nump">5,000</td></tr>
      <tr class="ro"><td class="pl">Common stock, shares issued and outstanding</td><td class="nump">2,500</td></tr>
      <tr class="ro"><td class="pl">Intangible assets</td><td class="nump">1,000</td></tr>
    </table></body></html>'''
    result = parse_r4_balance_sheet_bank(parse_r_html_table(html, r_file="R4.htm", url="https://sec.test/R4.htm"))
    assert result["total_equity"] == Decimal("28710000000")
    assert result["preferred_stock"] is None
    assert result["common_equity"] is None
    assert result["shares_ending_common"] is None
    share_table = html.replace("Common stock, shares issued and outstanding", "Common shares outstanding")
    assert parse_r4_balance_sheet_bank(parse_r_html_table(share_table, r_file="R4.htm", url="x"))["shares_ending_common"] is None
    share_table = share_table.replace("$ in Millions", "$ in Millions, shares in thousands")
    assert parse_r4_balance_sheet_bank(parse_r_html_table(share_table, r_file="R4.htm", url="x"))["shares_ending_common"] == 2_500_000


def test_valuation_inputs_require_matching_units_and_known_filing_date():
    from types import SimpleNamespace
    from app.fin_data.adapters.sec_bank_disclosure import _extract_latest_instant_from_snapshot

    row = {"end": "2025-09-30", "filed": "2025-10-16", "val": 91}
    units = {"EUR": [row], "USD": [{**row, "filed": None}, {**row, "filed": "2025-12-01"}]}
    snap = SimpleNamespace(facts_by_line={"equity": {"Equity": units}})
    def read():
        return _extract_latest_instant_from_snapshot(
            snap, "equity", required_unit="USD", as_of=date(2025, 11, 1),
            target_end=date(2025, 9, 30), max_end_drift_days=0,
        )[0]

    assert read() is None
    units["USD"].append({**row, "val": 12})
    assert read() == 12


def test_preferred_stock_par_value_label_does_not_override_statement_units():
    html = '''<html><body><table class="report">
      <tr><th class="tl">Balance Sheet - USD ($) $ in Millions</th><th class="th">Sep. 30, 2025</th></tr>
      <tr class="ro"><td class="pl">Total assets</td><td class="nump">455,312</td></tr>
      <tr class="ro"><td class="pl">Deposits</td><td class="nump">314,697</td></tr>
      <tr class="ro"><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_PreferredStockValue',window);">Preferred stock – par value $0.01 per share</a></td><td class="nump">4,836</td></tr>
      <tr class="ro"><td class="pl">Total stockholders' equity</td><td class="nump">43,879</td></tr>
      <tr class="ro"><td class="pl">Goodwill</td><td class="nump">16,773</td></tr>
      <tr class="ro"><td class="pl">Intangible assets</td><td class="nump">2,834</td></tr>
    </table></body></html>'''
    result = parse_r4_balance_sheet_bank(parse_r_html_table(html, r_file="R5.htm", url="https://sec.test/R5.htm"))
    assert result["preferred_stock"] == Decimal("4836000000")
    assert result["common_equity"] == Decimal("39043000000")


def test_period_rebinding_rejects_conflicts_and_old_quarter_and_annual_sections():
    from app.fin_data.adapters.sec_deep import EarningsReleaseExhibit, EarningsReleasePeriod, ParsedHtmlTable
    from app.fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics

    def extract(rows):
        table = ParsedHtmlTable(headers=["Consolidated results", "", ""], rows=rows)
        exhibit = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x", url="x", text="", tables=[table], content_sha256="x")
        period = EarningsReleasePeriod(period_role="current", accession="acc", filed_at=date(2025, 10, 16), period_of_report=date(2025, 10, 16), index_json_url="x", ex99_1=exhibit)
        return _extract_8k_supplement_metrics(period, date(2025, 9, 30))

    # The second quarterly section moves 3Q25 into another column.
    shifted = extract([
        ["", "3Q25", "2Q25"],
        ["Tangible book value per common share", "$30.60", "$29.57"],
        ["", "2Q25", "3Q25"],
        ["Tangible book value per common share", "$29.57", "$30.60"],
    ])
    assert shifted["tbvps_usd"] == Decimal("30.60")
    conflicting = extract([
        ["", "3Q25", "2Q25"],
        ["Tangible book value per common share", "$30.60", "$29.57"],
        ["", "2Q25", "3Q25"],
        ["Tangible book value per common share", "$29.57", "$31.00"],
    ])
    assert "tbvps_usd" not in conflicting
    assert "tbvps_usd_provenance" not in conflicting
    annual = extract([
        ["", "3Q25", "2Q25"],
        ["Tangible book value per common share", "$30.60", "$29.57"],
        ["Year ended", "2025", "2024"],
        ["Tangible book value per common share", "$31.64", "$27.05"],
    ])
    assert annual["tbvps_usd"] == Decimal("30.60")
    old_quarter = extract([
        ["", "3Q25", "2Q25"],
        ["", "2Q25", "1Q25"],
        ["Tangible book value per common share", "$29.57", "$28.00"],
    ])
    assert "tbvps_usd" not in old_quarter
