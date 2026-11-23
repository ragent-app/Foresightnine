"""Use the previously frozen observer's original strategy in both source trees."""
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch

import observer
from read_audit import wire_counts


def main():
    case, arm, path = sys.argv[1:]
    expected_dir = Path(__file__).resolve().parent / arm / 'apps' / 'api'
    assert Path(observer.runtime.__file__).resolve().is_relative_to(expected_dir)
    text = next(c['snapshot'] for c in observer.CASES['cases'] if c['id'] == case)
    real_provider = observer.prior.provider
    observations = []
    def provider():
        wrapped = real_provider()
        class Inspect:
            def __getattr__(self, name):
                return getattr(wrapped, name)
            def complete_turn(self, turn, **kwargs):
                wire = wrapped._wire(turn, cap=kwargs.get('max_output_tokens'))
                target = next((t for t in wire.get('tools', []) if t['name']=='calculate_financials'),None)
                masked = {**wire, 'tools': [{**t, 'input_schema':'<controlled>'} if t is target else t for t in wire.get('tools',[])]}
                masked_digest = sha256(json.dumps(masked,sort_keys=True).encode()).hexdigest()
                spans = set()
                for message in wire.get('messages', []):
                    blocks = message.get('content')
                    if not isinstance(blocks,list):
                        continue
                    for block in blocks:
                        if not isinstance(block,dict) or block.get('type') != 'tool_result' or not isinstance(block.get('content'), str):
                            continue
                        try:
                            value=json.loads(block['content'])
                        except ValueError:
                            continue
                        for row in value.get('results',[]) if isinstance(value,dict) else []:
                            if (isinstance(row,dict) and row.get('source_id')=='exhibit' and row.get('status')=='read'
                                    and type(row.get('start')) is int and type(row.get('end')) is int
                                    and 0<=row['start']<row['end']<=len(text) and row.get('text')==text[row['start']:row['end']]):
                                spans.add((row['start'],row['end']))
                if not observations:
                    check=json.loads((expected_dir.parents[2]/'preflight-result.json').read_text())
                    assert masked_digest==check[arm]['initial'][case], 'Initial wire outside controlled schema changed'
                rows=wire_counts(wire,spans,text)
                observations.append({**rows,'calc_schema_sha256':sha256(json.dumps(target,sort_keys=True).encode()).hexdigest() if target else None,
                                     'without_calc_sha256':masked_digest})
                return wrapped.complete_turn(turn, **kwargs)
        return Inspect()
    # Routing into observer arm A disables the old end-of-budget instruction in both processes.
    with patch.object(sys, 'argv', [sys.argv[0],case,'A',path]), patch.object(observer.prior, 'provider', provider):
        observer.main()
    result_path = Path(path)
    data = json.loads(result_path.read_text())
    assert all(not row['injected'] for row in data['build_observations'])
    for key in ('prompt_exposure','exposure_reason'):
        data.pop(key,None)
    data.update(arm=arm, wire_observations=observations)
    result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
