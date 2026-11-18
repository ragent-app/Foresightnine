"""Loopback-only, in-memory API for the 357 experiment; never import in production."""
import hmac
import json
import os
import sys
from fastapi import Header, HTTPException

if os.environ.get('FORESIGHTNINE_357_ISOLATED') != '1' or os.environ.get('FORESIGHTNINE_STORAGE_MODE') != 'memory' or os.environ.get('FORESIGHTNINE_ABUSE_MODE') != 'memory':
    raise RuntimeError('Isolated in-memory startup required')
import app.main as api
from app.research_diagnostics import safe_diagnostic_context, sanitize_transport_diagnostic, sanitize_diagnostic_errors

@api.app.get('/_357/private/{operation_id}')
def private_record(operation_id: str, x_experiment_token: str = Header(default='')):
    if not hmac.compare_digest(x_experiment_token, os.environ['FORESIGHTNINE_357_TOKEN']):
        raise HTTPException(404)
    record = api.RESEARCH_RUN_INPUTS.get(operation_id)
    if not isinstance(record, dict):
        raise HTTPException(404)
    diagnostics = record.get('private_validation_diagnostics', {})
    result = []
    if isinstance(diagnostics, dict):
        for call_id, value in list(diagnostics.items())[:256]:
            if not isinstance(value, dict): continue
            context = value.get('context') if isinstance(value.get('context'), dict) else {}
            item = safe_diagnostic_context(**{**context, 'call_id': call_id})
            for key, choices in {'finish': {'unknown', 'provider_length', 'provider_stop', 'provider_refusal'},
                                 'category': {'contract_validation', 'index_constraint', 'provider_shape', 'response_limit', 'envelope_parse', 'content_parse', 'provider_parse'}}.items():
                if value.get(key) in choices: item[key] = value[key]
            if 'transport' in value: item['transport'] = sanitize_transport_diagnostic(value['transport'])
            if isinstance(value.get('errors'), list):
                from app.research_analysis import AnalysisOutput
                from app.research_verification import ReviewOutput
                # Take the union of the two known schema views, without accepting
                # arbitrary path names or model-provided fields.
                analysis_errors = sanitize_diagnostic_errors(value['errors'], schema=AnalysisOutput)
                review_errors = sanitize_diagnostic_errors(value['errors'], schema=ReviewOutput)
                item['errors'] = [a if a.get('path', []).count('<unknown>') <= b.get('path', []).count('<unknown>') else b
                                  for a, b in zip(analysis_errors, review_errors)]
            exception = value.get('exception')
            if isinstance(exception, dict):
                name = exception.get('exception_class')
                location = exception.get('location')
                if isinstance(name, str) and name.isidentifier() and len(name) <= 100:
                    item['exception'] = {'exception_class': name}
                    if isinstance(location, dict) and isinstance(location.get('file'), str) and location['file'].endswith('.py') and location['file'].replace('_', '').replace('.py', '').isalnum() and type(location.get('line')) is int and 0 < location['line'] < 100000:
                        item['exception']['location'] = {'file': location['file'], 'line': location['line']}
            result.append(item)
    failures = []
    def collect_receipts(node):
        if isinstance(node, dict):
            receipts = node.get('model_call_receipts')
            if isinstance(receipts, (list, tuple)):
                for receipt in receipts:
                    if isinstance(receipt, dict) and receipt.get('diagnostic_status') == 'failed':
                        item = safe_diagnostic_context(call_id=receipt.get('call_id'))
                        if item: failures.append(item)
            for key, child in node.items():
                if key != 'model_call_receipts': collect_receipts(child)
        elif isinstance(node, (list, tuple)):
            for child in node: collect_receipts(child)
    collect_receipts(record.get('recovery_snapshot'))
    return {'diagnostics': result, 'diagnostic_failures': failures[:256]}

@api.app.get('/_357/config')
def configuration(x_experiment_token: str = Header(default='')):
    if not hmac.compare_digest(x_experiment_token, os.environ['FORESIGHTNINE_357_TOKEN']):
        raise HTTPException(404)
    provider = api.challenge_provider
    settings = getattr(provider, 'settings', None)
    generation = getattr(settings, 'glm_generation', None)
    params = {} if generation is None else {'thinking_enabled': generation.thinking_enabled, 'reasoning_effort': generation.reasoning_effort, 'do_sample': generation.do_sample}
    # This builds a local request envelope only; no network submission.
    try:
        wire = json.loads(provider._request_body(None, '', system=''))
        for key in ('temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'seed', 'reasoning_effort', 'do_sample'):
            if key in wire: params[key] = wire[key]
        if isinstance(wire.get('thinking'), dict): params['thinking'] = wire['thinking'].get('type')
        mode = 'tool' if 'tools' in wire else (wire.get('response_format') or {}).get('type', 'unknown')
    except Exception:
        mode = 'unknown'
    output = safe_diagnostic_context(provider=getattr(provider, 'name', 'unknown'), model=getattr(settings, 'model', None), parameters=params, output_mode=mode)
    policy = getattr(provider, 'policy', None)
    timeout = getattr(policy, 'timeout_seconds', None)
    if type(timeout) in (int, float) and 0 < timeout < 100000: output['request_timeout_seconds'] = timeout
    output['mode_scope'] = 'default_envelope_contract_mode_recorded_per_call_when_available'
    return output

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(api.app, host='127.0.0.1', port=int(sys.argv[1]), access_log=False, log_level='critical')
