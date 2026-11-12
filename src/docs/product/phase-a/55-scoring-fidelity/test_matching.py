from matching import allowed_percentage, mentions_no_dissent, mentions_usd_millions


def test_usd_amount_conversion_requires_exact_unit_and_value():
    assert mentions_usd_millions("营收为269.74亿美元", "26974")
    assert mentions_usd_millions("营收为26,914百万美元", "26914")
    assert not mentions_usd_millions("营收为269.74亿韩元", "26974")
    assert not mentions_usd_millions("营收为269.75亿美元", "26974")
    assert not mentions_usd_millions("营收为269,74亿美元", "26974")
    assert not mentions_usd_millions("269.74亿美元", "-26974")


def test_positive_magnitude_requires_unnegated_decrease():
    def check(text, number, allowed):
        return allowed_percentage(text, text.index(number), number, allowed)
    assert check("实际GDP下降了0.6%", "0.6", ["-0.6"])
    assert check("Real GDP decreased by 0.6 percent", "0.6", ["-0.6"])
    assert check("GDP为-0.6%", "-0.6", ["-0.6"])
    assert not check("GDP上升0.6%", "0.6", ["-0.6"])
    assert not check("GDP没有下降0.6%", "0.6", ["-0.6"])
    assert not check("GDP did not decrease 0.6%", "0.6", ["-0.6"])
    assert not check("GDP下降-0.6%", "-0.6", ["-0.6"])
    assert not check("GDP下降0.7%", "0.7", ["-0.6"])


def test_no_dissent_requires_affirmative_expression():
    assert mentions_no_dissent("没有委员投反对票")
    assert mentions_no_dissent("无人反对")
    assert mentions_no_dissent("No one voted against this action")
    assert not mentions_no_dissent("并非没有委员投反对票")
    assert not mentions_no_dissent("存在反对票")
    assert not mentions_no_dissent("无委员未投反对票")
    assert not mentions_no_dissent("The vote was not unanimous")


def test_scorer_uses_pinned_helpers_and_keeps_source_warnings(tmp_path):
    import hashlib
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for name in ("matching.py", "score.py"):
        shutil.copyfile(here / name, tmp_path / name)
    h = lambda b: hashlib.sha256(b).hexdigest()
    text = "Revenue 26974 million USD in FY2023."
    task = {"id": "T", "source_kind": "derived_text", "source_text": text}
    references = {"T": {"facts": [{"value": "26974", "period": "2023", "unit": "million", "literal": "26974"}],
                         "allowed_percentages": []}}
    for name, data in (("T-input.json", task), ("references.json", references)):
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False))
    manifest = {"assessment": "synthetic test", "inputs": [{"id": "T", "sha256": h((tmp_path / 'T-input.json').read_bytes())}],
                "scorer_sha256": h((tmp_path / 'score.py').read_bytes()), "matching_sha256": h((tmp_path / 'matching.py').read_bytes()),
                "references_sha256": h((tmp_path / 'references.json').read_bytes()), "max_tokens_per_task": 10000, "max_elapsed_seconds_per_task": 120}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    mh = h((tmp_path / 'manifest.json').read_bytes())
    (tmp_path / 'run-started.json').write_text(json.dumps({"manifest_sha256": mh}))
    source_hash = h(text.encode())
    citation = {"source_id": "s", "quotation": text, "location": {"character_start": 0, "character_end": len(text)}}
    fact = {"value": "26974", "period": "2023", "unit": "USD million", "currency": "USD", **citation}
    claim = {"text": "营收为269.74亿美元", "role": "support", "citations": [citation]}
    content = {"sources": [{"source_id": "s", "content_sha256": source_hash}], "facts": [fact], "claims": [claim],
               "limitations": ["No counterarguments were provided; this does not establish that none exist."],
               "run": {"source_hashes": {"s": source_hash}, "model_calls": 1, "search_calls": 0, "total_tokens": 10, "elapsed_seconds": 1}}
    result = {"manifest_sha256": mh, "operation_status": "completed", "content": content}
    def score():
        (tmp_path / 'T-result.json').write_text(json.dumps(result, ensure_ascii=False))
        subprocess.run([sys.executable, str(tmp_path / 'score.py')], check=True, capture_output=True)
        return json.loads((tmp_path / 'score.json').read_text())["tasks"]["T"]
    assert score()["mechanical_pass"] is True
    claim["citation_context_warnings"] = [{"status": "requires_review"}]
    assert score()["mechanical_pass"] is False
    claim.pop("citation_context_warnings")
    claim["text"] = "营收为269.75亿美元"
    assert score()["mechanical_pass"] is False
    with (tmp_path / "matching.py").open("a") as handle:
        handle.write("\n# altered after freezing\n")
    assert subprocess.run([sys.executable, str(tmp_path / "score.py")], capture_output=True).returncode != 0
