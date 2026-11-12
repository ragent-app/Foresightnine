from pathlib import Path
import json

from checks import comparison_quotations_cover_values, unexpected_explicit_usd_amounts, explicitly_distinguishes_periods


def cite(sid, text, part):
    a = text.index(part)
    return {"source_id": sid, "quotation": part, "location": {"character_start": a, "character_end": a + len(part)}}


def test_each_source_quote_must_contain_its_own_value():
    sources = {"a": "September 74.4 billion.", "b": "September 88.2 billion."}
    claim = {"text": "74.4 修订为 88.2", "citations": [cite("a", sources["a"], "September"), cite("b", sources["b"], "September")]}
    assert not comparison_quotations_cover_values(claim, sources, {"a": "74.4", "b": "88.2"})
    claim["citations"] = [cite("a", sources["a"], "74.4"), cite("b", sources["b"], "88.2")]
    assert comparison_quotations_cover_values(claim, sources, {"a": "74.4", "b": "88.2"})
    claim["citations"][0]["partial_token"] = True
    assert not comparison_quotations_cover_values(claim, sources, {"a": "74.4", "b": "88.2"})
    claim["citations"][0]["partial_token"] = False
    claim["citations"][1]["location"]["character_start"] += 1
    assert not comparison_quotations_cover_values(claim, sources, {"a": "74.4", "b": "88.2"})


def test_explicit_chinese_amount_scales_and_rounding():
    refs = ["74400", "88200", "13800"]  # USD millions
    assert unexpected_explicit_usd_amounts("74.4亿美元", refs) == ["74.4亿美元"]
    assert unexpected_explicit_usd_amounts("744亿美元、882亿美元、138亿美元", refs) == []
    assert unexpected_explicit_usd_amounts("74.4个十亿美元、0.882千亿美元", refs) == []
    assert unexpected_explicit_usd_amounts("74.4亿美元、74.4十亿美元", refs) == ["74.4亿美元"]
    assert unexpected_explicit_usd_amounts("744.0亿美元，约880亿美元", refs) == ["880亿美元"]
    assert unexpected_explicit_usd_amounts("74.4 euros, 88.2", refs) == []


def test_known_result_exposes_scoring_gaps_without_changing_its_saved_score():
    root = Path(__file__).resolve().parents[1] / "67-two-source-selection-check"
    result = json.loads((root / "result.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    sources = {sid: (root / source["text"]).read_text() for sid, source in zip(result["source_ids"], manifest["sources"])}
    claims = result["content"]["claims"]
    assert unexpected_explicit_usd_amounts(claims[0]["text"], ["74400", "88200", "13800"]) == ["74.4亿美元"]
    expected = dict(zip(result["source_ids"], ["74.4", "88.2"]))
    assert not comparison_quotations_cover_values(claims[2], sources, expected)
    assert json.loads((root / "score.json").read_text())["mechanical_pass"] is True


def test_period_difference_recognizes_direct_wording_without_generalizing_to_industry():
    for text in ("两家公司的财年并不覆盖完全相同的期间。", "两者的财年结束时间不同。",
                 "The fiscal years are different.", "The reporting periods are not the same."):
        assert explicitly_distinguishes_periods(text)
    for text in ("两家公司行业不同。", "财年并非不同。", "The fiscal years are not different.", "期间相同。"):
        assert not explicitly_distinguishes_periods(text)


def test_known_fiscal_wording_is_detected_without_rewriting_frozen_score():
    root = Path(__file__).resolve().parents[1] / "70-multitask-evaluation"
    result = json.loads((root / "FISCAL_COMPARE-result.json").read_text())
    assert explicitly_distinguishes_periods(result["content"]["claims"][1]["text"])
    assert json.loads((root / "score.json").read_text())["tasks"]["FISCAL_COMPARE"]["expected"]["different_fiscal_end_dates_with_both_quotes"] is False
