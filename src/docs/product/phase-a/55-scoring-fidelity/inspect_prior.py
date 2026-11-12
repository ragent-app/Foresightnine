"""Inspect specific known textual matches only; do not rescore previous runs."""
from pathlib import Path
from hashlib import sha256
import json
from matching import allowed_percentage, mentions_no_dissent, mentions_usd_millions
p=Path(__file__).resolve().parent
prior=p.parent/'54-research-instruction-paired'
h=lambda b:sha256(b).hexdigest()
for side in ('baseline','variant'):
 m=json.loads((prior/side/'manifest.json').read_text())
 assert h((prior/side/'score.py').read_bytes())==m['scorer_sha256']
 assert h((prior/side/'references.json').read_bytes())==m['references_sha256']
 report=json.loads((prior/side/'score.json').read_text())
 assert report['manifest_sha256']==h((prior/side/'manifest.json').read_bytes())
variant=json.loads((prior/'variant/NVDA_REV-result.json').read_text())['content']['claims'][0]['text']
bea=json.loads((prior/'baseline/BEA_Q3-result.json').read_text())['content']['claims']
bea=next(row['text'] for row in bea if '0.6%' in row['text'])
fed=json.loads((prior/'baseline/FED_VOTE-result.json').read_text())['content']['claims'][0]['text']
out={'kind':'post-run textual inspection only; not a rescoring and not independent acceptance',
 'original_score_sha256':{side:h((prior/side/'score.json').read_bytes()) for side in ('baseline','variant')},
 'cases':{'usd_2023_conversion':mentions_usd_millions(variant,'26974'),
          'usd_2022_conversion':mentions_usd_millions(variant,'26914'),
          'bea_decrease_magnitude':allowed_percentage(bea,bea.index('0.6'), '0.6',['-0.6']),
          'fed_no_dissent_phrase':mentions_no_dissent(fed)},
 'limitations':'Textual matches do not prove source support, period identity or overall task correctness.'}
print(json.dumps(out,ensure_ascii=False,indent=2))
