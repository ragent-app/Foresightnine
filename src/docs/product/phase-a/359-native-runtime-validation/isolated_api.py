"""Loopback-only, in-memory API for the 359 experiment; never import in production."""
import hmac
import json
import os
import sys
from fastapi import Header, HTTPException

if os.environ.get('FORESIGHTNINE_359_ISOLATED') != '1' or os.environ.get('FORESIGHTNINE_STORAGE_MODE') != 'memory' or os.environ.get('FORESIGHTNINE_ABUSE_MODE') != 'memory':
    raise RuntimeError('Isolated in-memory startup required')
import app.main as api
from app.research_diagnostics import safe_diagnostic_context, sanitize_transport_diagnostic, sanitize_diagnostic_errors, sanitize_provider_error

@api.app.get('/_359/private/{operation_id}')
def private_record(operation_id: str, x_experiment_token: str = Header(default='')):
    if not hmac.compare_digest(x_experiment_token, os.environ['FORESIGHTNINE_359_TOKEN']):
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
                from app.research_acquisition import ProposedAction
                # Take the union of the two known schema views, without accepting
                # arbitrary path names or model-provided fields.
                analysis_errors = sanitize_diagnostic_errors(value['errors'], schema=AnalysisOutput)
                review_errors = sanitize_diagnostic_errors(value['errors'], schema=ReviewOutput)
                acquisition_errors = sanitize_diagnostic_errors(value['errors'], schema=ProposedAction)
                item['errors'] = [min(group, key=lambda error: error.get('path', []).count('<unknown>'))
                                  for group in zip(analysis_errors, review_errors, acquisition_errors)]
            if isinstance(value.get('provider_error'), dict):
                item['provider_error'] = sanitize_provider_error(value['provider_error'])
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
    return {'diagnostics': result, 'diagnostic_failures': failures[:256], 'recovery_snapshot': record.get('recovery_snapshot'), 'runtime_checkpoint': record.get('runtime_checkpoint'), 'recovery_pending': record.get('recovery_pending')}

@api.app.get('/_359/config')
def configuration(x_experiment_token: str = Header(default='')):
    if not hmac.compare_digest(x_experiment_token, os.environ['FORESIGHTNINE_359_TOKEN']):
        raise HTTPException(404)
    provider = api.challenge_provider
    settings = getattr(provider, 'settings', None)
    # Only explicit, non-secret fields are recorded; the key never enters this object.
    import hashlib
    params = json.loads(os.environ['FORESIGHTNINE_LLM_GENERATION_PARAMETERS'])
    profile = json.loads(os.environ['FORESIGHTNINE_LLM_CAPABILITY_PROFILE'])
    endpoint = settings.base_url.rstrip('/') + '/chat/completions' if settings else None
    output = {'provider': getattr(provider, 'name', 'unknown'), 'protocol': 'openai_chat_completions',
              'model': getattr(settings, 'model', None), 'endpoint': endpoint,
              'allowed_hosts': sorted([f'{host}:{port}' for host, port in settings.allowed_hosts]) if settings else [],
              'native_tools': profile.get('native_tools'), 'capability_status': 'unverified',
              'continuation': profile.get('continuation'), 'continuation_fields': profile.get('continuation_fields'),
              'parameters': params, 'generation_parameter_allowlist': profile.get('generation_parameters'),
              'endpoint_version': profile.get('version'), 'request_timeout_seconds': getattr(provider.policy, 'timeout_seconds', None)}
    canonical = json.dumps(output, sort_keys=True, separators=(',', ':')).encode()
    output['configuration_sha256'] = hashlib.sha256(canonical).hexdigest()
    return output

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(api.app, host='127.0.0.1', port=int(sys.argv[1]), access_log=False, log_level='critical')
