"""Step V3a-1 comprehensive 0-LLM unit tests: OOXML parsers, parse_transcript_v2, Direction A D6 hook,
Layer 1 Sec10QCollisionLedger (initial-basis anti-restatement & P+1 hindsight), and Layer 3 7-track collision."""
from __future__ import annotations

from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import time
import zipfile

import pytest

from app.research_blob_store import DocumentBlobStore
from app.research_call_collision import (
    AccountVerbalCommitmentLedger,
    ExecutiveQuarterBaseline,
    ExtractedExpectationSignal,
    ExtractedInternalContradiction,
    ExtractedQaEvasion,
    ExtractedSkepticNote,
    ExtractedVerbalCommitment,
    Layer2ManagementExtraction,
    Layer2SellsideExtraction,
    ManagementFactualClaim,
    RedlineRiskSnippet,
    VerbalCommitmentRecord,
    build_sec_10q_collision_ledger,
    render_call_verification_markdown,
    run_layer3_call_collision,
    screen_item1a_omissions,
    xbrl_actuals_lookup,
)
from app.research_docx import extract_docx
from app.research_peer_compare import PeerCompanySnapshot
from app.research_pptx import OoxmlSecurityError, extract_pptx
from app.research_sources import ResearchSource, SourcePermissions
from app.research_transcript_parser import (
    build_direction_a_transcript_index_from_user_source,
    classify_document_archetype,
    clean_subtitle_text_with_timestamps,
    load_v3a_call_rules,
    normalize_entity_key,
    parse_transcript_v2,
)
from app.research_upload_stream import ingest_uploaded_file, sniff_upload_kind


# --------------------------------------------------------------------------- Helper Builders for OOXML Archives

def _make_zip_bytes(entries: dict[str, bytes | str], compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as zf:
        for name, data in entries.items():
            zf.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
    return buf.getvalue()


def _make_sample_pptx_bytes() -> bytes:
    """Build a realistic PPTX with reordered slides (slide2 before slide1), Chart (% & billions), Notes, Hidden Slide, Merged Table, Visual-only Slide."""
    pres_xml = """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:sldIdLst>
    <p:sldId id="256" r:id="rId2"/>
    <p:sldId id="257" r:id="rId1"/>
    <p:sldId id="258" r:id="rId3"/>
    <p:sldId id="259" r:id="rId4"/>
  </p:sldIdLst>
</p:presentation>"""
    pres_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide3.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide4.xml"/>
</Relationships>"""
    # Slide 2 comes FIRST in presentation order! Has chart + speaker notes
    slide2 = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Q3 FY26 Financial Highlights &amp; Guidance</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>"""
    slide2_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdN" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide2.xml"/>
  <Relationship Id="rIdC1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>
  <Relationship Id="rIdC2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart2.xml"/>
</Relationships>"""
    notes2 = """<?xml version="1.0" encoding="UTF-8"?>
<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
         xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>CFO note: Exclude one-time supply write-down of $450M.</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
</p:notes>"""
    chart1_pct = """<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
  <c:chart><c:plotArea>
    <c:barChart><c:ser>
      <c:tx><c:v>Gross Margin</c:v></c:tx>
      <c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>Q3 FY26</c:v></c:pt></c:strCache></c:strRef></c:cat>
      <c:val><c:numRef><c:numCache>
        <c:formatCode>0.0%</c:formatCode>
        <c:pt idx="0"><c:v>0.752</c:v></c:pt>
      </c:numCache></c:numRef></c:val>
    </c:ser></c:barChart>
  </c:plotArea></c:chart>
</c:chartSpace>"""
    chart2_billions = """<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
  <c:chart><c:plotArea>
    <c:barChart><c:ser>
      <c:tx><c:v>Data Center Revenue</c:v></c:tx>
      <c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>Q3 FY26</c:v></c:pt></c:strCache></c:strRef></c:cat>
      <c:val><c:numRef><c:numCache>
        <c:formatCode>$#,##0.0</c:formatCode>
        <c:pt idx="0"><c:v>45.2</c:v></c:pt>
      </c:numCache></c:numRef></c:val>
    </c:ser></c:barChart>
    <c:valAx><c:dispUnits><c:builtInUnit val="billions"/></c:dispUnits></c:valAx>
  </c:plotArea></c:chart>
</c:chartSpace>"""
    # Slide 1 comes SECOND in presentation order, with a merged table (gridSpan=2)
    slide1 = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p><a:r><a:t>Segment Breakdown Table</a:t></a:r></a:p></p:txBody></p:sp>
    <p:graphicFrame><a:graphic><a:graphicData><a:tbl>
      <a:tr>
        <a:tc gridSpan="2"><a:txBody><a:p><a:r><a:t>FY26 Revenue Summary</a:t></a:r></a:p></a:txBody></a:tc>
        <a:tc hMerge="1"><a:txBody><a:p/></a:txBody></a:tc>
      </a:tr>
      <a:tr>
        <a:tc><a:txBody><a:p><a:r><a:t>Compute</a:t></a:r></a:p></a:txBody></a:tc>
        <a:tc><a:txBody><a:p><a:r><a:t>$38.0B</a:t></a:r></a:p></a:txBody></a:tc>
      </a:tr>
    </a:tbl></a:graphicData></a:graphic></p:graphicFrame>
  </p:spTree></p:cSld>
</p:sld>"""
    # Slide 3: hidden slide (show="0")
    slide3_hidden = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld show="0" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Internal Backup Sensitivity Margin 69.5%</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>"""
    # Slide 4: visual-only slide (<30 chars + <a:blip>)
    slide4_visual = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld><p:spTree>
    <p:pic><p:blipFill><a:blip r:embed="rIdImg1"/></p:blipFill></p:pic>
  </p:spTree></p:cSld>
</p:sld>"""
    return _make_zip_bytes(
        {
            "[Content_Types].xml": "<Types/>",
            "ppt/presentation.xml": pres_xml,
            "ppt/_rels/presentation.xml.rels": pres_rels,
            "ppt/slides/slide1.xml": slide1,
            "ppt/slides/slide2.xml": slide2,
            "ppt/slides/_rels/slide2.xml.rels": slide2_rels,
            "ppt/notesSlides/notesSlide2.xml": notes2,
            "ppt/charts/chart1.xml": chart1_pct,
            "ppt/charts/chart2.xml": chart2_billions,
            "ppt/slides/slide3.xml": slide3_hidden,
            "ppt/slides/slide4.xml": slide4_visual,
        }
    )


def _make_sample_docx_bytes() -> bytes:
    doc_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>NVDA Q3 FY26 Management Call Notes: Gross margin was </w:t></w:r>
      <w:del w:id="1"><w:r><w:delText>82.0% DELETED DRAFT</w:delText></w:r></w:del>
      <w:ins w:id="2"><w:r><w:t>74.6% on GAAP basis.</w:t></w:r></w:ins>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Metric</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Q3 FY26</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>$35.1B</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
    return _make_zip_bytes(
        {
            "[Content_Types].xml": "<Types/>",
            "word/document.xml": doc_xml,
        }
    )


# --------------------------------------------------------------------------- 1. OOXML Security & Fidelity Tests

def test_pptx_fidelity_reordering_charts_notes_hidden_merged_visual(tmp_path: Path):
    p = tmp_path / "sample.pptx"
    p.write_bytes(_make_sample_pptx_bytes())
    res = extract_pptx(p)
    assert res.slide_count == 4
    assert res.hidden_slides == [3]
    assert res.visual_only_slides == [4]
    assert res.document_pages["method"] == "pptx_slides_v1"
    assert res.document_pages["page_count"] == 4

    # Presentation order: Slide 2 XML must appear as Slide 1 before Segment Breakdown Table (Slide 2)
    slides_split = [s for s in res.text.split("\f") if s]
    assert len(slides_split) == 4
    assert "Q3 FY26 Financial Highlights" in slides_split[0]
    assert "75.2%" in slides_split[0]
    assert "45.2" in slides_split[0] and "billions" in slides_split[0]
    assert "[Slide 1 Notes]" in slides_split[0] and "$450M" in slides_split[0]
    assert "Segment Breakdown Table" in slides_split[1]
    assert "FY26 Revenue Summary" in slides_split[1] and "$38.0B" in slides_split[1]
    assert "Hidden Slide" in slides_split[2] and "69.5%" in slides_split[2]


def test_pptx_rejects_zip_bomb_xxe_and_path_traversal(tmp_path: Path):
    # 1. Zip bomb: huge zeros compressed tiny (>50x ratio / exceeds 4MB member cap)
    bomb_path = tmp_path / "bomb.pptx"
    bomb_path.write_bytes(
        _make_zip_bytes(
            {
                "[Content_Types].xml": "<Types/>",
                "ppt/presentation.xml": "A" * 5_000_000,
            }
        )
    )
    with pytest.raises(OoxmlSecurityError):
        extract_pptx(bomb_path)

    # 2. XXE DOCTYPE/ENTITY rejection
    xxe_path = tmp_path / "xxe.pptx"
    xxe_path.write_bytes(
        _make_zip_bytes(
            {
                "[Content_Types].xml": "<Types/>",
                "ppt/presentation.xml": '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><p:presentation/>',
            }
        )
    )
    with pytest.raises(OoxmlSecurityError, match="DOCTYPE|ENTITY|disallowed"):
        extract_pptx(xxe_path)

    # 3. Path traversal in presentation.xml.rels
    trav_path = tmp_path / "trav.pptx"
    trav_path.write_bytes(
        _make_zip_bytes(
            {
                "[Content_Types].xml": "<Types/>",
                "ppt/presentation.xml": '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="1" r:id="rId1"/></p:sldIdLst></p:presentation>',
                "ppt/_rels/presentation.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="../../etc/passwd"/></Relationships>',
            }
        )
    )
    with pytest.raises(OoxmlSecurityError, match="traversal|Disallowed|outside"):
        extract_pptx(trav_path)


def test_docx_skips_deleted_revisions_and_keeps_inserts_and_tables(tmp_path: Path):
    doc_path = tmp_path / "sample.docx"
    doc_path.write_bytes(_make_sample_docx_bytes())
    res = extract_docx(doc_path)
    assert "DELETED DRAFT" not in res.text
    assert "74.6% on GAAP basis." in res.text
    assert "| Metric | Q3 FY26 |" in res.text
    assert "| Revenue | $35.1B |" in res.text

    xxe_path = tmp_path / "xxe.docx"
    xxe_path.write_bytes(
        _make_zip_bytes(
            {
                "[Content_Types].xml": "<Types/>",
                "word/document.xml": '<!DOCTYPE doc [<!ENTITY x "boom">]><w:document/>',
            }
        )
    )
    with pytest.raises(OoxmlSecurityError, match="DOCTYPE|ENTITY|disallowed"):
        extract_docx(xxe_path)


def test_streaming_upload_ingests_pptx_docx_vtt_blobstore_zero_base64(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    blob_store = DocumentBlobStore(tmp_path / "blobs")
    monkeypatch.setattr("app.research_blob_store.configured_blob_store", lambda: blob_store)
    monkeypatch.setattr("app.research_fetch.configured_blob_store", lambda: blob_store)
    perms = SourcePermissions(store=True, model_input=True, display=True, export=True)

    pptx_bytes = _make_sample_pptx_bytes()
    pptx_path = tmp_path / "q3_deck.pptx"
    pptx_path.write_bytes(pptx_bytes)
    assert sniff_upload_kind(pptx_path, "q3_deck.pptx") == "pptx"
    r_pptx = ingest_uploaded_file(
        pptx_path,
        kind="pptx",
        title="NVDA Q3 Deck",
        permissions=perms,
        sha256_hex="1" * 64,
        size=len(pptx_bytes),
    )
    assert r_pptx.kind == "pptx"
    assert r_pptx.original_base64 is None  # Zero Base64 in Postgres when BlobStore configured!
    assert r_pptx.document.pages["method"] == "pptx_slides_v1"
    assert r_pptx.document.pages["original_blob_sha256"] is not None

    docx_bytes = _make_sample_docx_bytes()
    docx_path = tmp_path / "notes.docx"
    docx_path.write_bytes(docx_bytes)
    assert sniff_upload_kind(docx_path, "notes.docx") == "docx"
    r_docx = ingest_uploaded_file(
        docx_path,
        kind="docx",
        title="NVDA Notes",
        permissions=perms,
        sha256_hex="2" * 64,
        size=len(docx_bytes),
    )
    assert r_docx.kind == "docx"
    assert "DELETED DRAFT" not in r_docx.document.text
    assert r_docx.original_base64 is None

    vtt_path = tmp_path / "call.vtt"
    vtt_path.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nOperator: Welcome.\n", encoding="utf-8")
    assert sniff_upload_kind(vtt_path, "call.vtt") == "text"


# --------------------------------------------------------------------------- 2. Layer 0 parse_transcript_v2 & Direction A D6

def test_vtt_rolling_dedup_and_timestamp_preservation():
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:04.000
<v Operator>Good afternoon and welcome to the Q3 FY26 earnings call.

00:00:04.000 --> 00:00:08.000
<v Operator>Good afternoon and welcome to the Q3 FY26 earnings call.
<v Colette Kress>Unless otherwise stated, all margins and EPS are Non-GAAP.

00:00:08.000 --> 00:00:12.000
<v Colette Kress>Unless otherwise stated, all margins and EPS are Non-GAAP.
Revenue grew strongly in Data Center.
"""
    clean, anchors = clean_subtitle_text_with_timestamps(vtt)
    assert clean.count("Good afternoon and welcome") == 1
    assert clean.count("Unless otherwise stated") == 1
    assert any(ts == "00:00:04" for _off, ts in anchors)


def test_parse_transcript_v2_6_formats_default_basis_slide_refs_and_negative_filters():
    # Negative filter: 10-K must NEVER auto-trigger V3a
    ten_k = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nFORM 10-K\nIndicate by check mark whether the registrant (1) has filed all reports..."
    c_10k = classify_document_archetype(ten_k, title="NVDA 10-K")
    assert c_10k.archetype == "form_10k_10q"
    assert c_10k.auto_trigger_eligible is False

    contract = "THIS AGREEMENT IS MADE AND ENTERED INTO as of May 1, 2026.\nWHEREAS, the parties agree.\nNOW, THEREFORE, in consideration of the mutual covenants."
    c_legal = classify_document_archetype(contract, title="MSA")
    assert c_legal.archetype == "legal_contract"
    assert c_legal.auto_trigger_eligible is False

    # Multilingual transcript with opening Non-GAAP declaration, Safe Harbor, slide ref, and multi-part Q&A
    transcript = """NVIDIA Corporation (NASDAQ: NVDA) Q3 FY26 Earnings Conference Call
November 20, 2025

Company Participants
Jensen Huang - Chief Executive Officer
Colette Kress - Chief Financial Officer

Conference Call Participants
Stacy Rasgon - Bernstein
Toshiya Hari - Goldman Sachs

Prepared Remarks

Operator: Good afternoon. Forward-looking statements are subject to the Private Securities Litigation Reform Act and actual results may differ materially. Unless otherwise stated, all gross margin, operating expense, and EPS figures discussed on this call are Non-GAAP.

Colette Kress: Turning to slide 14, Data Center revenue reached $30.8 billion and channel inventory remains very healthy across all regions.

Questions and Answers

Operator: Our first question comes from Stacy Rasgon with Bernstein.

Stacy Rasgon (Bernstein): Thanks guys. Two questions: first, what percent of Blackwell shipments went to top two hyperscalers? Second, how many basis points of gross margin headwind do you see in Q4?

Jensen Huang: We are broadly excited about the long-term opportunity and well positioned across every cloud partner, though we do not break out individual customer shares.

Operator: Next question comes from Toshiya Hari from Goldman Sachs.

[00:28:15] Toshiya Hari (Goldman Sachs): Could you quantify Q4 Blackwell gross margin trajectory and CapEx supply commitments?

Colette Kress: Gross margin will be roughly 73% in Q4 as Blackwell ramps.
"""
    parsed = parse_transcript_v2(
        transcript,
        source_id="src_nvda_q3",
        title="NVDA Q3 FY26 Call",
    )
    assert parsed.classification.archetype == "transcript"
    assert parsed.classification.auto_trigger_eligible is True
    assert parsed.call_default_basis == "non_gaap"
    assert parsed.detected_symbol == "NVDA"
    assert parsed.call_fiscal_year == 2026
    assert parsed.call_fiscal_quarter == 3
    assert parsed.call_date_iso == "2025-11-20"
    assert 14 in parsed.slide_references_map
    assert len(parsed.qa_exchanges) == 2
    assert parsed.qa_exchanges[0].multi_part_question_hit is True
    assert parsed.qa_exchanges[0].asked_quantitative is True
    assert parsed.qa_exchanges[0].answered_quantitative is False
    assert parsed.qa_exchanges[1].timestamp_start == "00:28:15"
    # Raw offset invariant: slicing untouched transcript matches speaker turn text
    for t in parsed.turns:
        raw_slice = transcript[t.raw_char_start : t.raw_char_end]
        assert t.text[:20] in raw_slice or t.speaker_name in raw_slice

    # Chinese Q&A format
    zh_call = """腾讯控股 2025年第三季度业绩说明会
2025年11月13日
问答环节
【提问】请问视频号广告加载率目前具体多少？毛利率节奏如何？
【回答】整体符合预期，长期看好商业化潜力，暂时不便透露具体比例。
"""
    zh_parsed = parse_transcript_v2(zh_call, source_id="src_zh", title="腾讯 Q3 电话会")
    assert zh_parsed.call_fiscal_year == 2025
    assert zh_parsed.call_fiscal_quarter == 3
    assert len(zh_parsed.qa_exchanges) == 1

    # Non-SEC entity normalization + alias map (D1)
    assert normalize_entity_key(symbol="0700.HK") == "nonsec:HKEX:0700"
    assert normalize_entity_key(symbol="600519.SH") == "nonsec:SSE:600519"
    assert normalize_entity_key(company_name="腾讯控股", alias_map={"腾讯控股": "nonsec:HKEX:0700"}) == "nonsec:HKEX:0700"

    # Direction A D6 transcript_index reuse preserves user export=True (never forged to fmp_transcript / export=False)
    user_src = ResearchSource(
        source_id="src_nvda_q3",
        owner_id="acct_1",
        title="NVDA Q3 FY26 Call",
        text=transcript,
        kind="user_supplied",
        retrieval_method="user_text",
        is_simulated=False,
        obtained_at=datetime.now(timezone.utc),
        content_sha256="a" * 64,
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )
    tr_snap, _ = build_direction_a_transcript_index_from_user_source(user_src, symbol="NVDA")
    assert tr_snap is not None
    assert tr_snap.provider == "user_uploaded_v3a"
    assert tr_snap.source_id == "src_nvda_q3"
    assert tr_snap.permissions["export"] is True
    assert "prepared_remarks" in tr_snap.sections and "qa" in tr_snap.sections
    assert "Colette Kress" in tr_snap.speakers and "Jensen Huang" in tr_snap.speakers


# --------------------------------------------------------------------------- 3. Layer 1 Initial-Basis Anti-Restatement & P+1 Hindsight

def _make_nvda_like_snapshot() -> PeerCompanySnapshot:
    """Synthetic NVDA-like snapshot with:
    - Q3 FY26 (ended 2025-10-26, initial 10-Q filed 2025-11-21)
    - Later 10-K/A restatement/split filed 2026-03-01 changing Q3 FY26 EPS from 8.00 -> 0.80 (10:1 split!)
    - Q4 FY26 (P+1, ended 2026-01-25, filed 2026-02-26) for immediate P+1 hindsight grading
    - Split OpEx (rd_expense + selling_expense + admin_expense)
    - High inventory growth vs COGS for Track 1 testing
    """
    return PeerCompanySnapshot(
        cik="0001045810",
        symbol="NVDA",
        company_name="NVIDIA CORP",
        sic="3674",
        sic_description="SEMICONDUCTORS",
        latest_periodic_accession="0001045810-26-000012",
        latest_periodic_filed_at="2026-02-26",
        taxonomy="us-gaap",
        currency="USD",
        projection_version=4,
        facts_by_line={
            "revenue": {
                "Revenues": {
                    "USD": [
                        {"start": "2024-07-29", "end": "2024-10-27", "val": 20_000_000_000, "filed": "2024-11-20", "form": "10-Q", "accn": "0001-24-001", "fy": 2025, "fp": "Q3"},
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 35_000_000_000, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                        {"start": "2025-10-27", "end": "2026-01-25", "val": 36_000_000_000, "filed": "2026-02-26", "form": "10-K", "accn": "0001-26-001", "fy": 2026, "fp": "Q4"},
                    ]
                }
            },
            "cost_of_revenue": {
                "CostOfRevenue": {
                    "USD": [
                        {"start": "2024-07-29", "end": "2024-10-27", "val": 6_000_000_000, "filed": "2024-11-20", "form": "10-Q", "accn": "0001-24-001", "fy": 2025, "fp": "Q3"},
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 10_500_000_000, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                        {"start": "2025-10-27", "end": "2026-01-25", "val": 11_520_000_000, "filed": "2026-02-26", "form": "10-K", "accn": "0001-26-001", "fy": 2026, "fp": "Q4"},
                    ]
                }
            },
            "gross_profit": {
                "GrossProfit": {
                    "USD": [
                        {"start": "2024-07-29", "end": "2024-10-27", "val": 14_000_000_000, "filed": "2024-11-20", "form": "10-Q", "accn": "0001-24-001", "fy": 2025, "fp": "Q3"},
                        # GAAP GM = 70.0% in Q3 FY26 (24.5B / 35B)
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 24_500_000_000, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                        # GAAP GM = 68.0% in Q4 FY26 (24.48B / 36B)
                        {"start": "2025-10-27", "end": "2026-01-25", "val": 24_480_000_000, "filed": "2026-02-26", "form": "10-K", "accn": "0001-26-001", "fy": 2026, "fp": "Q4"},
                    ]
                }
            },
            "eps_diluted": {
                "EarningsPerShareDiluted": {
                    "USD/shares": [
                        # Initial 10-Q reported pre-split EPS = $8.00
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 8.00, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                        # Later post-split 10-K/A restated comparative Q3 FY26 EPS to $0.80
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 0.80, "filed": "2026-03-01", "form": "10-K/A", "accn": "0001-26-099", "fy": 2026, "fp": "Q3"},
                    ]
                }
            },
            "inventory": {
                "InventoryNet": {
                    "USD": [
                        {"start": None, "end": "2024-10-27", "val": 5_000_000_000, "filed": "2024-11-20", "form": "10-Q", "accn": "0001-24-001", "fy": 2025, "fp": "Q3"},
                        # Inventory grew +100% YoY vs COGS +75% YoY -> +25pp divergence (red >= 20pp)
                        {"start": None, "end": "2025-10-26", "val": 10_000_000_000, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                    ]
                }
            },
            "rd_expense": {
                "ResearchAndDevelopmentExpense": {
                    "USD": [
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 3_400_000_000, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                    ]
                }
            },
            "selling_expense": {
                "SellingAndMarketingExpense": {
                    "USD": [
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 600_000_000, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                    ]
                }
            },
            "admin_expense": {
                "GeneralAndAdministrativeExpense": {
                    "USD": [
                        {"start": "2025-07-28", "end": "2025-10-26", "val": 300_000_000, "filed": "2025-11-21", "form": "10-Q", "accn": "0001-25-003", "fy": 2026, "fp": "Q3"},
                    ]
                }
            },
        },
    )


def test_initial_basis_anti_restatement_split_opex_and_p1_hindsight():
    snap = _make_nvda_like_snapshot()
    submissions = {
        "filings": {
            "recent": {
                "form": ["10-Q", "10-K", "10-K/A"],
                "accessionNumber": ["0001-25-003", "0001-26-001", "0001-26-099"],
                "filingDate": ["2025-11-21", "2026-02-26", "2026-03-01"],
                "reportDate": ["2025-10-26", "2026-01-25", "2025-10-26"],
            }
        }
    }

    # Verify split OpEx (3.4B + 0.6B + 0.3B = 4.3B) works without gross_profit/operating_income
    opex_res = xbrl_actuals_lookup(
        snap,
        "sga_or_opex",
        target_fy=2026,
        target_fq=3,
        basis_lock="gaap",
        initial_filing_cutoff=date(2025, 11, 21),
        as_of=date(2026, 4, 1),
    )
    assert opex_res.status == "matched"
    assert opex_res.value == "4300000000.00"

    # Initial-basis EPS must be 8.00 (NOT post-split 0.80!), with restated_later=True transparently noted!
    eps_res = xbrl_actuals_lookup(
        snap,
        "eps_diluted",
        target_fy=2026,
        target_fq=3,
        basis_lock="gaap",
        initial_filing_cutoff=date(2025, 11, 21),
        as_of=date(2026, 4, 1),
    )
    assert eps_res.status == "matched"
    assert eps_res.value == "8.0000"
    assert eps_res.initial_basis_value == "8.0000"
    assert eps_res.latest_value == "0.8000"
    assert eps_res.restated_later is True

    # Build ledger at as_of=2026-04-01 -> sees P (Q3 FY26) and P+1 (Q4 FY26)
    ledger = build_sec_10q_collision_ledger(
        entity_key="0001045810",
        snap=snap,
        submissions=submissions,
        call_fy=2026,
        call_fq=3,
        call_date_iso="2025-11-20",
        as_of=date(2026, 4, 1),
    )
    assert ledger.status == "same_period_10q_matched"
    assert ledger.initial_10q_filed_at == "2025-11-21"
    assert ledger.p1_fiscal_year == 2026 and ledger.p1_fiscal_quarter == 4
    assert ledger.p1_10q_filed_at == "2026-02-26"
    assert ledger.inv_minus_cogs_pp == "25.00"

    # Earlier as_of=2025-12-01 must NOT see P+1 (anti-lookahead!)
    ledger_early = build_sec_10q_collision_ledger(
        entity_key="0001045810",
        snap=snap,
        submissions=submissions,
        call_fy=2026,
        call_fq=3,
        call_date_iso="2025-11-20",
        as_of=date(2025, 12, 1),
    )
    assert ledger_early.p1_10q_filed_at is None


# --------------------------------------------------------------------------- 4. Layer 3 7-Track Collision, Confidentiality, Item 1A & Hot Rules

def test_layer3_7track_nongaap_lock_conscious_incompetence_confidentiality_item1a_and_hot_rules(tmp_path: Path):
    snap = _make_nvda_like_snapshot()
    raw_call = (
        "NVIDIA Q3 FY26 Earnings Call\nNovember 20, 2025\n"
        "Operator: Unless otherwise stated, all margins and EPS discussed on this call are Non-GAAP.\n"
        "Jensen Huang: Channel inventory is extremely lean and demand is broad across every vertical. Our GAAP EPS reached $8.00 this quarter. We are seeing nice momentum in robotics.\n"
        "Colette Kress: Gross margin reached 75.0% in Q3. However, Q4 GAAP gross margin will be at least 73.0%, and supply constraints are easing.\n"
        "Questions and Answers\n"
        "Stacy Rasgon (Bernstein): What is your single customer pricing with Microsoft?\n"
        "Colette Kress: As a policy we do not disclose single customer pricing or NDA contract terms.\n"
        "Toshiya Hari (Goldman Sachs): First, what is Q4 Blackwell margin? Second, what is your China export tariff exposure?\n"
        "Jensen Huang: We are excited about the AI factory era and cannot share confidential roadmap details, let's talk about our gaming launch instead.\n"
    )
    parsed = parse_transcript_v2(raw_call, source_id="src_call", title="NVDA Q3 FY26")
    assert parsed.call_default_basis == "non_gaap"

    submissions = {
        "filings": {
            "recent": {
                "form": ["10-Q", "10-K", "10-K/A"],
                "accessionNumber": ["0001-25-003", "0001-26-001", "0001-26-099"],
                "filingDate": ["2025-11-21", "2026-02-26", "2026-03-01"],
                "reportDate": ["2025-10-26", "2026-01-25", "2025-10-26"],
            }
        }
    }
    ledger = build_sec_10q_collision_ledger(
        entity_key="0001045810",
        snap=snap,
        submissions=submissions,
        call_fy=2026,
        call_fq=3,
        call_date_iso="2025-11-20",
        as_of=date(2026, 4, 1),
        redline_added_paragraphs=[
            # Covered via export_control bucket (call mentions "tariff" & "export", 10-Q says "customs duties")
            {"section_key": "item_1a_risk_factors", "current_text": "New customs duties and export control licenses could restrict shipment of our GPUs to China."},
            # Truly unmentioned cybersecurity breach risk -> MUST flag!
            {"section_key": "item_1a_risk_factors", "current_text": "We recently experienced an unauthorized access and ransomware security incident affecting internal ERP servers."},
        ],
        redline_current_accession="0001-25-003",
        redline_prior_accession="0001-24-001",
    )

    # Prior quarter commitment ledger (for Silent Drop + Speaker-Baseline Shift)
    prior_ledger = AccountVerbalCommitmentLedger(
        owner_id="acct_1",
        entity_key="0001045810",
        commitments=[
            VerbalCommitmentRecord(
                commitment_id="cmt_old_automotive",
                owner_id="acct_1",
                entity_key="0001045810",
                source_id="src_q2",
                source_fiscal_period="FY2026-Q2",
                call_date="2025-08-20",
                metric_canonical="other",
                kpi_key="AutomotivePipeline",
                verification_tier="operating_kpi_next_call",
                status="pending_next_call",
                is_high_profile_theme=True,
                theme_key="AutomotivePipeline",
                commitment_text="Automotive design-win pipeline will cross $15B in H2.",
            )
        ],
        executive_baselines=[
            ExecutiveQuarterBaseline(
                fiscal_period_key="FY2026-Q2",
                call_date="2025-08-20",
                executive_name="Jensen Huang",
                speaker_role="ceo",
                answer_count=4,
                avg_hedge_density=0.002,
                avg_verbosity_ratio=1.2,
                evasion_rate_pct=10.0,
            )
        ],
    )

    l2 = Layer2ManagementExtraction(
        owner_id="acct_1",
        entity_key="0001045810",
        fiscal_period_key="FY2026-Q3",
        mgmt_bundle_sha256="b" * 64,
        factual_claims=[
            # 1) Implicit Non-GAAP (basis="unknown" inherits call_default_basis="non_gaap") -> MUST NOT hard-collide red!
            ManagementFactualClaim(
                metric_canonical="gross_margin",
                basis="unknown",
                period_scope="single_quarter",
                target_fy=2026,
                target_fq=3,
                operator="approx",
                value_low="75.0",
                unit="%",
                evidence_sufficiency="explicit",
                track_hint="earnings_cash",
                source_id="src_call",
                verbatim_quote="Gross margin reached 75.0% in Q3.",
                claim_summary="Q3 gross margin reached 75.0%",
            ),
            # 2) Pre-split GAAP EPS $8.00 -> MUST corroborate against initial 10-Q ($8.00), NOT falsely collide with post-split $0.80!
            ManagementFactualClaim(
                metric_canonical="eps_diluted",
                basis="gaap",
                period_scope="single_quarter",
                target_fy=2026,
                target_fq=3,
                operator="approx",
                value_low="8.00",
                unit="USD/shares",
                evidence_sufficiency="explicit",
                track_hint="earnings_cash",
                source_id="src_call",
                verbatim_quote="Our GAAP EPS reached $8.00 this quarter.",
                claim_summary="Q3 GAAP EPS reached $8.00",
            ),
            # 3) Channel inventory healthy -> collides with +25pp Inventory vs COGS!
            ManagementFactualClaim(
                metric_canonical="other",
                evidence_sufficiency="explicit",
                qualitative_stance="bullish_healthy",
                track_hint="demand_channel",
                source_id="src_call",
                verbatim_quote="Channel inventory is extremely lean",
                claim_summary="Channel inventory is lean",
            ),
            # 4) Conscious Incompetence: vague implied claim -> MUST stay info!
            ManagementFactualClaim(
                metric_canonical="revenue",
                basis="gaap",
                value_low="40",
                scale="billions",
                evidence_sufficiency="implied",
                track_hint="earnings_cash",
                source_id="src_call",
                verbatim_quote="We are seeing nice momentum in robotics.",
                claim_summary="Implied robotics revenue ramp",
            ),
        ],
        verbal_commitments=[
            # Q4 FY26 GAAP GM >= 73% -> P+1 Q4 10-K actual is 68.0% -> MUST grade missed immediately!
            ExtractedVerbalCommitment(
                metric_canonical="gross_margin",
                basis="gaap",
                period_scope="single_quarter",
                target_fy=2026,
                target_fq=4,
                operator="ge",
                value_low="73.0",
                unit="%",
                evidence_sufficiency="explicit",
                source_id="src_call",
                verbatim_quote="Q4 GAAP gross margin will be at least 73.0%",
                commitment_text="Q4 FY26 GAAP gross margin >= 73.0%",
            )
        ],
        qa_evasions=[
            # Legitimate confidentiality (both flags True + NOT topic_pivot) -> whitelisted info!
            ExtractedQaEvasion(
                exchange_index=1,
                analyst_name="Stacy Rasgon",
                executive_name="Colette Kress",
                executive_role="cfo",
                question_asks_sensitive_detail=True,
                exec_cites_confidentiality_policy=True,
                evasion_level="fully_evasive",
                behavior_subtype="omission",
                source_id="src_call",
                question_quote="What is your single customer pricing with Microsoft?",
                answer_quote="As a policy we do not disclose single customer pricing or NDA contract terms.",
                rationale="Customer-level pricing NDA",
            ),
            # Abuse of "confidential" + topic_pivot -> MUST NOT whitelist, stays red!
            ExtractedQaEvasion(
                exchange_index=2,
                analyst_name="Toshiya Hari",
                executive_name="Jensen Huang",
                executive_role="ceo",
                sub_questions=["Q4 Blackwell margin", "China export tariff exposure"],
                unanswered_sub_questions=["Q4 Blackwell margin", "China export tariff exposure"],
                question_asks_sensitive_detail=True,
                exec_cites_confidentiality_policy=True,
                evasion_level="fully_evasive",
                behavior_subtype="topic_pivot",
                source_id="src_call",
                question_quote="First, what is Q4 Blackwell margin? Second, what is your China export tariff exposure?",
                answer_quote="let's talk about our gaming launch instead.",
                rationale="Pivoted to gaming while invoking confidential shield",
            ),
        ],
        internal_contradictions=[
            ExtractedInternalContradiction(
                topic="Demand vs Margin",
                speaker_a_name="Jensen Huang",
                speaker_a_role="ceo",
                source_a_id="src_call",
                quote_a="Channel inventory is extremely lean",
                speaker_b_name="Colette Kress",
                speaker_b_role="cfo",
                source_b_id="src_call",
                quote_b="Q4 GAAP gross margin will be at least 73.0%",
                contradiction_summary="CEO bullish inventory posture vs CFO margin caution",
            )
        ],
        expectation_signals=[
            ExtractedExpectationSignal(
                kind="analyst_cluster",
                topic="Blackwell margin",
                analyst_count=3,
                analyst_names=["Stacy Rasgon", "Toshiya Hari", "Vivek Arya"],
                source_id="src_call",
                verbatim_quote="what is Q4 Blackwell margin?",
                summary="3 analysts pressed on Q4 Blackwell gross margin trajectory",
            )
        ],
        skeptic_notes=[ExtractedSkepticNote(target_ref="inventory", note="Inventory build may reflect Blackwell pre-launch staging.")],
    )

    sellside = Layer2SellsideExtraction(
        owner_id="acct_1",
        entity_key="0001045810",
        source_id="src_ss",
        content_sha256="c" * 64,
        sellside_firm="Bernstein",
        sellside_timing="pre_earnings_preview",
        expectation_signals=[
            ExtractedExpectationSignal(
                kind="sellside_expectation",
                topic="Q3 Revenue Preview",
                sellside_firm="Bernstein",
                sellside_timing="pre_earnings_preview",
                source_id="src_ss",
                verbatim_quote="We model $34.5B revenue.",
                summary="Sell-side modeled $34.5B revenue ahead of Q3 print.",
            )
        ],
    )

    report, updated_ledger = run_layer3_call_collision(
        entity_key="0001045810",
        parsed_docs=[parsed],
        ledger=ledger,
        snap=snap,
        mgmt_extraction=l2,
        sellside_extractions=[sellside],
        source_texts={"src_call": raw_call, "src_ss": "Bernstein preview: We model $34.5B revenue."},
        source_permissions={
            "src_call": {"store": True, "model_input": True, "display": True, "export": True},
            "src_ss": {"store": True, "model_input": True, "display": True, "export": False},
        },
        existing_commitment_ledger=prior_ledger,
        as_of=date(2026, 4, 1),
        owner_id="acct_1",
    )

    # 1. Implicit Non-GAAP margin (75% vs 70% GAAP = 500 bps) -> basis_divergence_nongaap_gap (yellow), NEVER direct_contradiction red!
    ng = next(f for f in report.collision_findings if f.collision_kind == "basis_divergence_nongaap_gap")
    assert ng.severity == "yellow"
    assert "500 bps" in (ng.formula_note or "")

    # 2. Pre-split GAAP EPS $8.00 corroborated with restated_later=True!
    eps_ok = next(f for f in report.collision_findings if f.collision_kind == "corroborated" and "eps_diluted" in f.title)
    assert eps_ok.severity == "green"
    assert eps_ok.restated_later is True
    assert "0.8000" in (eps_ok.restatement_note or "")

    # 3. Conscious Incompetence implied claim -> info
    ci = next(f for f in report.collision_findings if f.collision_kind == "observation_only_insufficient_evidence")
    assert ci.severity == "info"

    # 4. CEO vs CFO divergence pinned at top (priority_rank=5, red)
    assert report.collision_findings[0].collision_kind == "ceo_cfo_divergence"
    assert report.collision_findings[0].severity == "red"

    # 5. Track 1 inventory contradiction red (+25pp >= 20pp)
    inv_hit = next(f for f in report.collision_findings if f.track == "track_1_demand_channel" and f.collision_kind == "direct_contradiction")
    assert inv_hit.severity == "red"

    # 6. Track 5 Item 1A dual-factor screening: customs/tariff NOT flagged; cybersecurity IS flagged!
    om_hits = [s for s in report.omission_phase2_candidates]
    assert len(om_hits) == 1
    assert "cybersecurity" in om_hits[0].risk_buckets

    # 7. D3 Confidentiality whitelist: exchange 1 whitelisted (info); exchange 2 topic_pivot stays RED!
    assert report.confidentiality_whitelisted_count == 1
    ev1 = next(q for q in report.qa_evasions if q.exchange_index == 1)
    ev2 = next(q for q in report.qa_evasions if q.exchange_index == 2)
    assert ev1.legitimate_confidentiality is True and ev1.display_severity == "info"
    assert ev2.legitimate_confidentiality is False and ev2.display_severity == "red"

    # 8. P+1 Hindsight grading: Q4 GAAP GM >= 73% vs actual 68.0% -> MISSED!
    q4_cmt = next(c for c in updated_ledger.commitments if c.metric_canonical == "gross_margin" and c.target_fq == 4)
    assert q4_cmt.status == "missed"
    assert q4_cmt.actual_value == "68.00"

    # 9. Chronological Silent Drop: prior Q2 AutomotivePipeline absent in Q3 -> silent_drop_high_risk!
    auto_cmt = next(c for c in updated_ledger.commitments if c.commitment_id == "cmt_old_automotive")
    assert auto_cmt.status == "silent_drop_high_risk"

    # 10. Export-aware Markdown brief: user transcript exported, export=False sell-side redacted!
    md = render_call_verification_markdown(
        report,
        source_permissions={
            "src_call": {"export": True},
            "src_ss": {"export": False},
        },
    )
    assert "Gross margin reached 75.0% in Q3." in md
    assert "受导出权限限制已脱敏" in md
    assert "We model $34.5B revenue." not in md

    # 11. Hot rules reload (<10ms zero-rebuild): raise inv_minus_cogs_pp_red from 20 -> 30 flips inv_hit from red -> yellow!
    base_rules_raw = json.loads(
        (Path(__file__).resolve().parents[1] / "app" / "fin_data" / "adapters" / "v3a_call_verification_rules.json").read_text("utf-8")
    )
    base_rules_raw["evaluation_rules_version"] = "v2-hot"
    base_rules_raw["ledger_collision_thresholds"]["inv_minus_cogs_pp_red"] = 30.0
    custom_rules_path = tmp_path / "rules_v2.json"
    custom_rules_path.write_text(json.dumps(base_rules_raw), encoding="utf-8")
    hot_rules = load_v3a_call_rules(custom_rules_path)

    t0 = time.perf_counter()
    report_v2, _ = run_layer3_call_collision(
        entity_key="0001045810",
        parsed_docs=[parsed],
        ledger=ledger,
        snap=snap,
        mgmt_extraction=l2,
        source_texts={"src_call": raw_call},
        existing_commitment_ledger=prior_ledger,
        as_of=date(2026, 4, 1),
        rules=hot_rules,
        owner_id="acct_1",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 100.0  # Typically <3ms
    assert report_v2.evaluation_rules_version == "v2-hot"
    inv_hit_v2 = next(f for f in report_v2.collision_findings if f.track == "track_1_demand_channel" and f.collision_kind == "direct_contradiction")
    assert inv_hit_v2.severity == "yellow"


def test_financial_sic_exempts_track1():
    jpm_snap = PeerCompanySnapshot(
        cik="0000019617",
        symbol="JPM",
        company_name="JPMORGAN CHASE & CO",
        sic="6021",
        sic_description="NATIONAL COMMERCIAL BANKS",
        taxonomy="us-gaap",
        currency="USD",
        projection_version=4,
    )
    ledger = build_sec_10q_collision_ledger(entity_key="0000019617", snap=jpm_snap)
    assert ledger.is_financial_gated is True
    assert ledger.status == "financial_sic_gated"
    report, _ = run_layer3_call_collision(
        entity_key="0000019617",
        parsed_docs=[],
        ledger=ledger,
        snap=jpm_snap,
        mgmt_extraction=None,
        source_texts={},
    )
    assert any(f.track == "track_1_demand_channel" and f.severity == "not_applicable" for f in report.collision_findings)
