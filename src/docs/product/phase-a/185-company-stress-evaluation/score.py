from common import *
import re

mb=(HERE/"manifest.json").read_bytes();m=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==v for f,v in m["files"].items())
assert all(digest((SOURCE/f).read_bytes())==v for f,v in m["source_files"].items())
assert json.loads((HERE/"run-started.json").read_text())["manifest_sha256"]==mh
path=HERE/"JPM_STRESS_2024-result.json"
if not path.exists(): result={"status":"not_run"}
else:
    out=json.loads(path.read_text());assert out["manifest_sha256"]==mh
    result={"status":out["status"]}
    if out["status"]=="completed":
        _,sources=get_task("JPM_STRESS_2024");source=sources[0];content=out.get("content",{});claims=content.get("claims",[])
        def valid(c):
            loc=c.get("location",{});a,b=loc.get("character_start"),loc.get("character_end")
            return (c.get("source_id")==source.source_id and type(a) is int and type(b) is int and
                    0<=a<b<=len(source.text) and source.text[a:b]==c.get("quotation") and not c.get("partial_token"))
        all_located=bool(claims) and all(c.get("citations") and all(valid(x) for x in c["citations"]) for c in claims)
        texts=" ".join(c.get("text","") for c in claims).casefold()
        quotes=" ".join(q.get("quotation","") for c in claims for q in c.get("citations",[]) if valid(q)).casefold()
        patterns={"bank":r"jpmorgan\s+chase", "metric":r"(?:common\s+equity\s+tier\s+1|cet1)",
                  "actual":r"\b15\.0\b", "projected_end":r"\b13\.4\b", "projected_minimum":r"\b12\.5\b"}
        checks={key:bool(re.search(pattern,texts,re.I) and re.search(pattern,quotes,re.I)) for key,pattern in patterns.items()}
        checks["actual_and_projection_labels"]=bool(re.search(r"actual",texts) and re.search(r"project",texts) and
            re.search(r"actual",quotes) and re.search(r"project",quotes) and re.search(r"minimum",quotes))
        checks["scenario_cited"]=bool(re.search(r"severely\s+adverse",texts) and re.search(r"severely\s+adverse",quotes))
        checks["all_claims_uniquely_located"]=all_located
        run=content.get("run",{});result.update(research_status=run.get("status"),mechanical_checks=checks,
            mechanical_pass=all(checks.values()),model_calls=run.get("model_calls"),reported_tokens=run.get("total_tokens"),
            review_status=run.get("support_review_status"),review_omissions=run.get("support_review_omissions"),
            claim_count=len(claims),semantic_support="not independently established; table alignment requires review")
(HERE/"score.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(result,ensure_ascii=False,indent=2))
