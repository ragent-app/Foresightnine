"""Offline snapshot and non-secret protocol checks; never print configuration values."""
import hashlib, json, os
from pathlib import Path
here=Path(__file__).resolve().parent
freeze=json.loads((here/'experiment-freeze.json').read_text())
for name,want in freeze.items():
    assert hashlib.sha256((here/name).read_bytes()).hexdigest()==want, name
manifest=json.loads((here/'e1-code-freeze.json').read_text())
for name,want in manifest['files_sha256'].items():
    path=here/name
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==want, name
assert hashlib.sha256((here/'cases.json').read_bytes()).hexdigest()==manifest['data_sha256']
assert hashlib.sha256((here/'prior.py').read_bytes()).hexdigest()=='92e3b9dc9e8e6a7a4f056b9fce3b02a257de839a4be1459aada333f686fa1d14'
assert os.environ.get('FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL') == manifest['protocol']
assert os.environ.get('FORESIGHTNINE_RESEARCH_LLM_MODEL') == manifest['model']
assert os.environ.get('FORESIGHTNINE_RESEARCH_LLM_API_KEY')
assert os.environ.get('FORESIGHTNINE_MESSAGES_NATIVE_TOOLS') == os.environ.get('FORESIGHTNINE_MESSAGES_CONTINUATION') == 'true'
# Provider constructor performs the endpoint/capability/allowlist consistency checks, without I/O.
import prior
prior.provider()
config={k:os.environ.get(k) for k in manifest['config_fields']}
digests={hashlib.sha256(json.dumps(config,sort_keys=True,separators=sep).encode()).hexdigest() for sep in [(',',':'),(', ',': ')]}
assert manifest['non_secret_model_config_sha256'] in digests, 'non-secret E1 configuration changed'
print('snapshot and E1 provider configuration checks passed')
