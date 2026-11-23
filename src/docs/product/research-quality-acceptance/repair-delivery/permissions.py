"""Permission/version route checks against isolated synthetic fixture."""
import json
from fastapi.testclient import TestClient
from api_fixture import build, install
from app import main

topics,sources=build(); install(topics,sources); client=TestClient(main.app)
id='D1-A-2'; headers={'Authorization':id}; url='/v2/research/topics/'+id; export=url+'/versions/v2/export'
def snapshot(label):
 a=client.get(url,headers=headers); b=client.get(export,headers=headers)
 return {'case':label,'topic_status':a.status_code,'topic':a.json(),'export_status':b.status_code,'export':b.json()}
out=[snapshot('baseline')]
sources[id].permissions.display=False;out.append(snapshot('display_denied'));sources[id].permissions.display=True
sources[id].permissions.export=False;out.append(snapshot('export_denied'));sources[id].permissions.export=True
sources[id].content_sha256='0'*64;out.append(snapshot('version_mismatch'))
other=client.get(url,headers={'Authorization':'D1-A-1'});out.append({'case':'other_owner','topic_status':other.status_code})
print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
