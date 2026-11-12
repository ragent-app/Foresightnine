from common import *
import re

mb=(HERE/"manifest.json").read_bytes();manifest=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==h for f,h in manifest["files"].items())
assert all(digest((SOURCE/f).read_bytes())==h for f,h in manifest["source_files"].items())
assert json.loads((HERE/"run-started.json").read_text())["manifest_sha256"]==mh
results={}
for condition in manifest["conditions"]:
    name=condition["task"];path=HERE/f"{name}-result.json"
    if not path.exists():results[name]={"status":"not_run"};continue
    out=json.loads(path.read_text());assert out["manifest_sha256"]==mh
    if out["status"]!="completed":results[name]={"status":out["status"]};continue
    _,sources=get_task(name);source=sources[0];content=out.get("content",{});claims=content.get("claims",[])
    def valid(citation):
        loc=citation.get("location",{});a,b=loc.get("character_start"),loc.get("character_end")
        return (citation.get("source_id")==source.source_id and type(a) is int and type(b) is int and
                0<=a<b<=len(source.text) and source.text[a:b]==citation.get("quotation") and not citation.get("partial_token"))
    def supported(claim_patterns,quote_patterns):
        for claim in claims:
            text=claim.get("text","").casefold()
            if not all(re.search(p,text,re.I|re.S) for p in claim_patterns):continue
            for citation in claim.get("citations",[]):
                quote=citation.get("quotation","").casefold()
                if valid(citation) and all(re.search(p,quote,re.I|re.S) for p in quote_patterns):return True
        return False
    all_located=bool(claims) and all(c.get("citations") and all(valid(x) for x in c["citations"]) for c in claims)
    if name=="PCE_2023_03":
        checks={
          "nominal_change":supported([r"8\.2",r"pce|consumption"],[r"8\.2",r"pce|consumption"]),
          "goods_change":supported([r"36\.7",r"goods",r"decreas|fell|fall|declin"],[r"36\.7",r"goods",r"decreas|fell|fall|declin"]),
          "services_change":supported([r"44\.9",r"services",r"increas|rose|rise"],[r"44\.9",r"services",r"increas|rose|rise"]),
          "saving_rate":supported([r"5\.1",r"sav"],[r"5\.1",r"sav"]),
          "real_less_than":supported([r"real",r"less than\s+0\.1",r"decreas|fell|fall|declin"],
                                     [r"real",r"less than\s+0\.1",r"decreas|fell|fall|declin"]),
        }
    else:
        lo=r"4\s*(?:-\s*1\s*/\s*2|\.5(?:0)?)";hi=r"4\s*(?:-\s*3\s*/\s*4|\.75)"
        checks={"target_range":supported([lo,hi,r"target|funds"],[lo,hi,r"target|funds"]),
                "reserve_rate":supported([r"4\.65",r"reserv"],[r"4\.65",r"reserv"]),
                "reserve_effective_date":supported([r"february\s+2|2023-02-02",r"reserv"],
                                                   [r"february\s+2",r"reserv"])}
    checks["all_claims_uniquely_located"]=all_located
    run=content.get("run",{});results[name]={"status":out["status"],"research_status":run.get("status"),
      "mechanical_checks":checks,"mechanical_pass":all(checks.values()),"model_calls":run.get("model_calls"),
      "reported_tokens":run.get("total_tokens"),"review_status":run.get("support_review_status"),
      "review_omissions":run.get("support_review_omissions"),"claim_count":len(claims),
      "counterargument_count":sum(c.get("role")=="counterargument" for c in claims),"semantic_support":"not independently established"}
(HERE/"score.json").write_text(json.dumps(results,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(results,ensure_ascii=False,indent=2))
