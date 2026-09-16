#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == "--local" ]]; then
  FILE=${2:?local modified JSON path required}
  python3 - "$FILE" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); data=json.loads(p.read_text())
for product in ('foresightnine_pro','foresightnine_max'):
    plan=(data[product].get('basePlans') or [{}])[0]
    if plan.get('state')=='ACTIVE': plan['state']='INACTIVE'
    print(f'{product}_ROLLBACK_STATE={plan.get("state")}')
p.write_text(json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
print('ROLLBACK_STATUS=restored_nonpurchasable_behavior')
print('ROLLBACK_EXIT=0')
PY
  exit 0
fi
uv run --with google-auth --with requests python - <<'PY'
from google.auth.transport.requests import Request
from google.oauth2 import service_account
import requests
cred='/home/lxx/.config/google-play/service-account.json'; scope='https://www.googleapis.com/auth/androidpublisher'; pkg='com.foresightnine.mobile'; plan='monthly'
c=service_account.Credentials.from_service_account_file(cred,scopes=[scope]); c.refresh(Request())
h={'Authorization':f'Bearer {c.token}','Accept':'application/json','Content-Type':'application/json'}
base=f'https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{pkg}/subscriptions'
for product in ('foresightnine_pro','foresightnine_max'):
    current=requests.get(f'{base}/{product}',headers=h,timeout=30); current.raise_for_status()
    state=((current.json().get('basePlans') or [{}])[0]).get('state')
    if state=='ACTIVE':
        response=requests.post(f'{base}/{product}/basePlans/{plan}:deactivate',headers=h,json={},timeout=30)
        print(f'{product}_DEACTIVATE_HTTP={response.status_code}')
        response.raise_for_status()
    else:
        print(f'{product}_DEACTIVATE_HTTP=NOT_ACTIVE')
print('ROLLBACK_STATUS=restored_nonpurchasable_behavior')
print('ROLLBACK_EXIT=0')
PY
