from hashlib import sha256
from pathlib import Path
import json
import common  # Adds the versioned API package to the import path.
from app.research_verification import ReviewBatch, apply_partial_support_review

HERE = Path(__file__).resolve().parent
result_path = HERE/'COMPANY_CISCO_2024-result.json'
provider_path = HERE/'COMPANY_CISCO_2024-review-2-provider.json'
result = json.loads(result_path.read_text())['content']
run = result['run']
provider = json.loads(provider_path.read_text())
assert provider['prompt_sha256'] == run['support_review_prompt_sha256']
batch = ReviewBatch(prompt='', indexes=run['support_review_indexes'],
                    omitted=[x['claim_index'] for x in run['support_review_omissions']],
                    segments=[], omissions=run['support_review_omissions'])
claims, missing = apply_partial_support_review(result['claims'], batch, provider['payload'])
output = {'model_calls':0, 'result_sha256':sha256(result_path.read_bytes()).hexdigest(),
          'provider_output_sha256':sha256(provider_path.read_bytes()).hexdigest(),
          'prompt_sha256':provider['prompt_sha256'], 'submitted_indexes':batch.indexes,
          'omitted_indexes':batch.omitted, 'missing':missing,
          'review_records':{str(index):claims[index].get('support_review') for index in batch.indexes}}
(HERE/'review-replay.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(output,ensure_ascii=False))
