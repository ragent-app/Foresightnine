from common import *
import re

raw_manifest = (HERE / 'manifest.json').read_bytes(); m = json.loads(raw_manifest); mh = digest(raw_manifest)
assert all(digest((HERE / name).read_bytes()) == expected for name, expected in m['files'].items())
assert json.loads((HERE / 'run-started.json').read_text())['manifest_sha256'] == mh
results = []
warning_fields = ('uncited_exact_dates', 'publication_date_warnings', 'rate_unit_warnings', 'uncited_amounts',
    'uncited_claim_amounts', 'uncited_dollar_amounts', 'uncited_quarters', 'balance_direction_warnings',
    'uncited_percentages', 'uncited_percentage_bounds', 'reused_percentages', 'uncited_basis_points',
    'uncited_percentage_points', 'uncited_fact_values', 'citation_context_warnings', 'causal_attribution_unverified')
for condition in m['conditions']:
    name = condition['task']; path = HERE / (name + '-result.json')
    if not path.exists():
        results.append({'task': name, 'set': condition['set'], 'status': 'not_run', 'mechanical_pass': False}); continue
    output = json.loads(path.read_text()); assert output['manifest_sha256'] == mh
    _, source, raw = load_task(name)
    assert digest(raw) == condition['source_sha256'] and source.content_sha256 == condition['text_sha256']
    content = output.get('content', {}); claims = content.get('claims', [])
    def valid(citation):
        location = citation.get('location', {}); start, end = location.get('character_start'), location.get('character_end')
        return (citation.get('source_id') == source.source_id and type(start) is int and type(end) is int and
                0 <= start < end <= len(source.text) and source.text[start:end] == citation.get('quotation') and
                source.text.count(citation.get('quotation', '')) == 1 and
                not citation.get('partial_token') and location.get('status') != 'ambiguous')
    def cited(pattern, role=None):
        return any((role is None or claim.get('role') == role) and re.search(pattern, claim.get('text', ''), re.I | re.S) and
                   any(valid(q) and re.search(pattern, q.get('quotation', ''), re.I | re.S) for q in claim.get('citations', []))
                   for claim in claims)
    checks = {'all_claims_located': bool(claims) and all(c.get('citations') and c.get('support_status') != 'incomplete' and
              all(valid(q) for q in c['citations']) for c in claims),
              'no_claim_warnings': not any(c.get(k) for c in claims for k in warning_fields),
              'counterargument_cited': any(c.get('role') == 'counterargument' and c.get('citations') and
                                          all(valid(q) for q in c['citations']) for c in claims),
              'research_completed': content.get('run', {}).get('status') == 'completed',
              'budget': type(content.get('run', {}).get('total_tokens')) is int and
                        content['run']['total_tokens'] <= m['max_tokens_per_task'] and content['run'].get('model_calls', 99) <= 2}
    if name == 'COMPANY_CISCO_2024':
        checks.update(revenue_current=cited(r'53,?803'), revenue_previous=cited(r'56,?998'),
            calculation=any(str(c.get('value')) == '-5.61' and c.get('unit') == 'percent' and c.get('input_status') == 'matched'
                            for c in content.get('calculations', [])))
    elif name == 'MACRO_GDP_2022Q4':
        checks.update(q4=cited(r'\b2\.7\b'), q3=cited(r'\b3\.2\b'),
                      services=cited(r'services.{0,180}(?:increas|rose)|(?:increas|rose).{0,180}services'),
                      goods_counter=cited(r'goods.{0,180}(?:decreas|fell)|(?:decreas|fell).{0,180}goods', 'counterargument'))
    else:
        checks.update(target_lower=cited(r'(?<![\d.])1(?:\.0+)?(?![\d.])'),
                      target_upper=cited(r'1\s*(?:-|\s)\s*1\s*/\s*4|1\.25'),
                      dissenter=cited(r'kashkari', 'counterargument'),
                      preferred_unchanged=cited(r'maintain.{0,50}(?:existing|current)|(?:existing|current).{0,50}maintain', 'counterargument'))
    results.append({'task': name, 'set': condition['set'], 'status': output.get('status'), 'checks': checks,
                    'mechanical_pass': output.get('status') == 'completed' and all(checks.values()),
                    'reported_tokens': content.get('run', {}).get('total_tokens'),
                    'semantic_support': 'requires separate source-level assessment'})
summary = {'results': results, 'mechanical_pass': len(results) == len(m['conditions']) and all(x['mechanical_pass'] for x in results),
           'known_tokens': sum(x['reported_tokens'] for x in results if type(x.get('reported_tokens')) is int),
           'unknown_usage': any(type(x.get('reported_tokens')) is not int for x in results),
           'semantic_support': 'not established by numeric or lexical matches'}
(HERE / 'score.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False, indent=2))
