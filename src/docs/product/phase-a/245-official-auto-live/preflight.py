from pathlib import Path
from hashlib import sha256
from time import monotonic
import base64, json, sys
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.research_fetch import FetchFailure, FetchSourceRequest, fetch_official_text
from app.research_sources import SourcePermissions
url = 'https://www.bea.gov/news/2024/gross-domestic-product-fourth-quarter-and-year-2023-advance-estimate'
expected = '10970e0f520e4571372e2f5e5d121f92ad1448e7871c26297f46cd8bdc3cee76'
started = monotonic()
with (HERE/'preflight-started.json').open('x') as handle:
    json.dump({'url': url, 'expected_response_sha256': expected, 'max_fetches': 1, 'timeout_seconds': 15}, handle, indent=2)
try:
    result = fetch_official_text(FetchSourceRequest(url=url, title='BEA advance estimate',
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True)), timeout_seconds=15)
    raw = base64.b64decode(result.original_html_base64 or '', validate=True)
    assert result.response_sha256 == expected and sha256(raw).hexdigest() == expected
    (HERE/'source.html').write_bytes(raw)
    (HERE/'source.txt').write_text(result.text)
    out = {'status':'matched','response_sha256':expected,'text_sha256':sha256(result.text.encode()).hexdigest(),
           'text_chars':len(result.text),'elapsed_seconds':round(monotonic()-started,3),'model_calls':0}
except (FetchFailure, ValueError, AssertionError) as error:
    out = {'status':'failed','error_type':type(error).__name__,'elapsed_seconds':round(monotonic()-started,3),'model_calls':0}
(HERE/'preflight-result.json').write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out))
