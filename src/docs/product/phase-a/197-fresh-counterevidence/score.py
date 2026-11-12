from common import *
import re

mb=(HERE/"manifest.json").read_bytes();manifest=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==h for f,h in manifest["files"].items())
assert all(digest((SOURCE/f).read_bytes())==h for f,h in manifest["source_files"].items())
assert json.loads((HERE/"run-started.json").read_text())["manifest_sha256"]==mh
path=HERE/"PCE_COUNTER_2024-result.json"
if not path.exists(): result={"status":"not_run"}
else:
    out=json.loads(path.read_text());assert out["manifest_sha256"]==mh
    result={"status":out["status"]}
    if out["status"]=="completed":
        _,sources=get_task("PCE_COUNTER_2024");source=sources[0];content=out.get("content",{});claims=content.get("claims",[])
        def valid(c):
            loc=c.get("location",{});a,b=loc.get("character_start"),loc.get("character_end")
            return (c.get("source_id")==source.source_id and type(a) is int and type(b) is int and
                    0<=a<b<=len(source.text) and source.text[a:b]==c.get("quotation") and not c.get("partial_token"))
        def cited(role,patterns):
            for claim in claims:
                if role and claim.get("role")!=role:continue
                text=claim.get("text","")
                if not all(re.search(p,text,re.I|re.S) for p in patterns):continue
                for citation in claim.get("citations",[]):
                    quote=citation.get("quotation","")
                    if valid(citation) and all(re.search(p,quote,re.I|re.S) for p in patterns):return True
            return False
        checks={"total":cited(None,[r"39\.1",r"pce|consumption"]),
                "services":cited(None,[r"49\.1",r"services"]),
                "opposing_goods":cited("counterargument",[r"10\.0",r"goods",r"decreas|fell|fall|declin"]),
                "all_claims_located_and_complete":bool(claims) and all(c.get("citations") and c.get("support_status")!="incomplete" and all(valid(x) for x in c["citations"]) for c in claims)}
        run=content.get("run",{});result.update(research_status=run.get("status"),mechanical_checks=checks,
            mechanical_pass=all(checks.values()),model_calls=run.get("model_calls"),reported_tokens=run.get("total_tokens"),
            review_status=run.get("support_review_status"),review_omissions=run.get("support_review_omissions"),
            candidate_count=sum(len(c.get("suggested_fact_evidence",[]))+len(c.get("suggested_related_quotations",[])) for c in claims),
            semantic_support="not independently established")
(HERE/"score.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(result,ensure_ascii=False,indent=2))
