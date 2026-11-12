"""Predefined narrow coverage signals; never a semantic support oracle."""
from common import *
import re


def score(name, out, source):
    content = out.get("content", {})
    claims = content.get("claims", [])
    def located(citation):
        location = citation.get("location", {})
        start, end = location.get("character_start"), location.get("character_end")
        quotation = citation.get("quotation")
        return (citation.get("source_id") == source.source_id and not citation.get("partial_token") and
                location.get("status") != "ambiguous" and type(start) is int and type(end) is int and
                isinstance(quotation, str) and 0 <= start < end <= len(source.text) and source.text[start:end] == quotation)
    valid = [claim for claim in claims if claim.get("citations") and all(located(c) for c in claim["citations"])]
    text = " ".join(c.get("text", "") for c in valid)
    quote = " ".join(" ".join(c["quotation"].split()) for claim in valid for c in claim["citations"])
    has = lambda pattern, value: bool(re.search(pattern, value, re.I))
    if name == "PCE_2022_07":
        checks = {"requested_numbers_in_claims_and_quotes": all(has(r"(?<![\d.])" + number + r"(?![\d.])", value)
                   for number in (r"23\.7", r"33\.3", r"9\.6", r"5(?:\.0)?") for value in (text, quote)),
                  "goods_and_services_named": all(has(term, text) and has(term, quote) for term in ("goods", "services"))}
    else:
        checks = {"target_bounds_in_quotes": all(has(pattern, quote) for pattern in (r"1\s*-\s*1/2", r"1\s*-\s*3/4")),
                  "dissenter_in_claims_and_quotes": all(has(r"Esther\s+(?:L\.\s*)?George", value) for value in (text, quote)),
                  "alternative_in_quotes": has(r"1\s*-\s*1/4", quote) and has(r"0\.5\s+percentage\s+point", quote),
                  "reserve_rate_in_claims_and_quotes": all(has(r"(?<!\d)1\.65(?!\d)", value) for value in (text, quote))}
    return {"mechanical_signals": checks, "all_claims_located": bool(claims) and len(valid) == len(claims),
            "research_status": content.get("run", {}).get("status"), "model_calls": content.get("run", {}).get("model_calls"),
            "reported_tokens": content.get("run", {}).get("total_tokens"), "semantic_support": "not independently established"}


def main():
    mb = (HERE / "manifest.json").read_bytes()
    m = json.loads(mb)
    assert json.loads((HERE / "run-started.json").read_text())["manifest_sha256"] == digest(mb)
    assert all(digest((HERE / f).read_bytes()) == h for f, h in m["files"].items())
    results = {}
    for condition in m["conditions"]:
        name = condition["task"]
        result_path = HERE / (name + "-result.json")
        if not result_path.exists():
            results[name] = {"status": "not_run"}
            continue
        out = json.loads(result_path.read_text())
        assert out["manifest_sha256"] == digest(mb)
        _, sources = get_task(name)
        results[name] = score(name, out, sources[0]) if out["status"] == "completed" else {"status": out["status"]}
    (HERE / "score.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
