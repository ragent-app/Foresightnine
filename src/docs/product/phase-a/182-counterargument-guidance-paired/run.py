from common import *
from dataclasses import asdict, replace
from threading import Event
from time import monotonic
import os
import subprocess
from app.research_analysis import SYSTEM_PROMPT
from app.phase3 import build_llm_provider, ProviderFailure
from app.openai_compatible import OpenAICompatibleLlmProvider

mb=(HERE/"manifest.json").read_bytes(); m=json.loads(mb); mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==v for f,v in m["files"].items())
assert all(digest((SOURCE/f).read_bytes())==v for f,v in m["source_files"].items())
assert digest(SYSTEM_PROMPT.encode())==m["system_sha256"]
subprocess.run(["git","diff","--quiet",m["implementation_commit"],"--","apps"],cwd=ROOT,check=True)
for line in json.loads(subprocess.check_output(["docker","inspect","--format","{{json .Config.Env}}","docker-api-1"])):
    key,sep,value=line.partition("=")
    if sep and (key.startswith("FORESIGHTNINE_LLM_") or key=="FORESIGHTNINE_GLM_CODING_API_KEY"): os.environ[key]=value
os.environ["FORESIGHTNINE_LLM_TIMEOUT_SECONDS"]="60"
p=build_llm_provider(); assert isinstance(p,OpenAICompatibleLlmProvider) and p.settings.model==m["model"]
params=json.loads(p._request_body(None,"probe",system="probe")); params.pop("messages")
assert params==m["generation_parameters"] and p.policy.max_output_tokens==m["output_limit"]
p=OpenAICompatibleLlmProvider(p.settings,policy=replace(p.policy,max_input_tokens=6000,max_attempts=1),transport=p.transport)
task,sources=get_task("PCE_2022_07")
with (HERE/"run-started.json").open("x") as f: json.dump({"manifest_sha256":mh,"status":"started_once"},f,indent=2)
for condition in m["conditions"]:
    name=condition["name"]; prompt=(HERE/f"{name}-prompt.json").read_text(); assert digest(prompt.encode())==condition["prompt_sha256"]
    start=monotonic(); out={"condition":name,"manifest_sha256":mh,"prompt_sha256":condition["prompt_sha256"],"cost_usd":"unknown"}
    try:
        raw,used=p.generate_structured(prompt,cancel_event=Event(),system=SYSTEM_PROMPT,timeout_seconds=60)
        analysis=run_analysis(task,sources,lambda _:(raw,0))
        out.update(status="completed",reported_tokens=used,raw=raw,result=asdict(analysis))
    except Exception as error:
        out.update(status="failure",error=error.code if isinstance(error,ProviderFailure) else type(error).__name__,
                   reported_tokens=error.reported_tokens if isinstance(error,ProviderFailure) else None)
    out["elapsed_seconds"]=round(monotonic()-start,3); serialized=json.dumps(out,ensure_ascii=False,indent=2)
    for key in ("FORESIGHTNINE_LLM_API_KEY","FORESIGHTNINE_GLM_CODING_API_KEY"):
        if os.environ.get(key):serialized=serialized.replace(os.environ[key],"[REDACTED]")
    (HERE/f"{name}-result.json").write_text(serialized+"\n")
    print(json.dumps({"condition":name,"status":out["status"],"reported_tokens":out.get("reported_tokens")}),flush=True)
    if out["status"]!="completed" or type(out.get("reported_tokens")) is not int or out["reported_tokens"]>10000:break
