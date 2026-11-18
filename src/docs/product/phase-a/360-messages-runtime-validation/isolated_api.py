"""Loopback-only experiment diagnostics: bounded metadata, no messages or bodies."""
import hmac, os, sys
from fastapi import Header, HTTPException
if os.environ.get('FORESIGHTNINE_360_ISOLATED') != '1' or os.environ.get('FORESIGHTNINE_STORAGE_MODE') != 'memory' or os.environ.get('FORESIGHTNINE_ABUSE_MODE') != 'memory':
    raise RuntimeError('Isolated memory configuration required')
import app.main as api
from app.research_diagnostics import safe_diagnostic_context, sanitize_transport_diagnostic, safe_messages_error

@api.app.get('/_360/diagnostics/{operation_id}')
def diagnostics(operation_id: str, x_experiment_token: str = Header(default='')):
    if not hmac.compare_digest(x_experiment_token, os.environ['FORESIGHTNINE_360_TOKEN']): raise HTTPException(404)
    record=api.RESEARCH_RUN_INPUTS.get(operation_id)
    if not isinstance(record,dict): raise HTTPException(404)
    calls=[]
    for call_id, value in list((record.get('private_validation_diagnostics') or {}).items())[:256]:
        if not isinstance(value,dict): continue
        context=value.get('context') if isinstance(value.get('context'),dict) else {}
        row=safe_diagnostic_context(**{**context,'call_id':call_id})
        row['transport']=sanitize_transport_diagnostic(value.get('transport'))
        error=value.get('provider_error') if isinstance(value.get('provider_error'),dict) else {}
        row['provider_response']=safe_messages_error(error,error.get('status_code'),{'request-id':error.get('request_id')})
        calls.append(row)
    checkpoint=record.get('runtime_checkpoint') or {}
    allowed={'read_sources','record_facts','calculate_financials','submit_research','discover_sources','fetch_official_sources'}
    events=[]
    for event in checkpoint.get('events',[])[:512]:
        if not isinstance(event,dict): continue
        kind=event.get('kind')
        if kind=='tool' and event.get('name') in allowed:
            row={'kind':'tool','name':event['name']}
            if event.get('status') in {'success','failed','completed'}: row['status']=event['status']
            row.update(safe_diagnostic_context(call_id=event.get('tool_call_id')))
            events.append(row)
        elif kind in {'model','model_failure','submission'}:
            events.append({'kind':kind,**safe_diagnostic_context(call_id=event.get('call_id'))})
    receipts=(checkpoint.get('_accounting') or {}).get('model_call_receipts')
    known=isinstance(receipts,dict) and all(isinstance(r,dict) and r.get('settlement') in {'settled','released'} for r in receipts.values())
    return {'calls':calls,'events':events,'pending':bool(checkpoint.get('pending')),'receipts_settled':known}

if __name__=='__main__':
    import uvicorn
    uvicorn.run(api.app,host='127.0.0.1',port=int(sys.argv[1]),access_log=False,log_level='critical')
