from common import *
import re

mb=(HERE/"manifest.json").read_bytes();m=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==v for f,v in m["files"].items())
assert all(digest((SOURCE/f).read_bytes())==v for f,v in m["source_files"].items())
assert json.loads((HERE/"run-started.json").read_text())["manifest_sha256"]==mh
_,sources=get_task("PCE_2022_07"); source=sources[0]; results={}
for condition in m["conditions"]:
    name=condition["name"];path=HERE/f"{name}-result.json"
    if not path.exists():results[name]={"status":"not_run"};continue
    out=json.loads(path.read_text()); assert out["manifest_sha256"]==mh
    if out["status"]!="completed":results[name]={"status":out["status"]};continue
    matches=[]
    for i,claim in enumerate(out["result"]["counterarguments"]):
        text=claim["text"].casefold()
        for citation in claim.get("citations",[]):
            quote=citation.get("quotation","").casefold();loc=citation.get("location",{})
            start,end=loc.get("character_start"),loc.get("character_end")
            located=(type(start) is int and type(end) is int and source.text[start:end]==citation.get("quotation"))
            quote_hit=all(term in quote for term in ("9.6","goods")) and bool(re.search(r"decreas|fell|fall|declin",quote))
            claim_hit="goods" in text and bool(re.search(r"decreas|fell|fall|declin",text))
            if located and quote_hit and claim_hit:matches.append(i)
    results[name]={"status":"completed","counterarguments":len(out["result"]["counterarguments"]),
                   "specific_counterargument_indexes":matches,"mechanical_pass":bool(matches)}
(HERE/"score.json").write_text(json.dumps(results,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(results,ensure_ascii=False,indent=2))
