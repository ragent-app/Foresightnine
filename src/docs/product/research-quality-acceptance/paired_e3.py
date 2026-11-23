"""One preregistered E3 arm. Runs only when explicitly invoked, one call maximum."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import time

from app.model_protocol import ModelToolDefinition, ModelTurnInput

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('prior_paired', HERE.parent / 'research-delivery-acceptance' / 'paired.py')
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)
CASES = json.loads((HERE / 'cases.json').read_text())
assert sha256((HERE / 'cases.json').read_bytes()).hexdigest() == '7e62e0cd385e9923ea1030b1fd9825b2fba41ade816d6601e8e57e288cdcd316'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('case', choices=['D1', 'D2'])
    p.add_argument('arm', choices=['A', 'B'])
    p.add_argument('output', type=Path)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    case = next(c for c in CASES['cases'] if c['id'] == args.case)
    base = {'type': 'object', 'properties': {'report': {'type': 'string'}}, 'required': ['report'], 'additionalProperties': False}
    bound = {'type': 'object', 'properties': {'report': {'type': 'string'}, 'claims': {'type': 'array', 'items': {'type': 'object', 'properties': {'text': {'type': 'string'}, 'evidence': {'type': 'string'}, 'calculation': {'type': 'string'}, 'limitations': {'type': 'string'}}, 'required': ['text', 'evidence', 'calculation', 'limitations'], 'additionalProperties': False}}}, 'required': ['report', 'claims'], 'additionalProperties': False}
    tool = ModelToolDefinition('submit_answer', 'Submit your answer to the question using only the exhibit.', base if args.arm == 'A' else bound)
    user = 'Question: ' + case['question'] + '\nExhibit: ' + case['snapshot']
    turn = ModelTurnInput('Answer the question using only the exhibit. Submit your answer using the tool.', user, (tool,), ())
    request_hash = sha256(json.dumps(asdict(turn), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if args.dry_run:
        print(json.dumps({'case': args.case, 'arm': args.arm, 'request_sha256': request_hash}))
        return
    args.output.with_suffix('.started').touch(exist_ok=False)
    start = time.monotonic()
    try:
        response = prior.provider().complete_turn(turn)
        result = {'case': args.case, 'arm': args.arm, 'request_sha256': request_hash,
                  'tool_calls': [asdict(x) for x in response.tool_calls], 'usage': asdict(response.usage) if response.usage is not None else None,
                  'finish_reason': response.finish_reason, 'elapsed_seconds': round(time.monotonic() - start, 3),
                  'actual_cost': None}
    except Exception as error:
        result = {'case': args.case, 'arm': args.arm, 'request_sha256': request_hash,
                  'error_type': type(error).__name__, 'elapsed_seconds': round(time.monotonic() - start, 3), 'actual_cost': None}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
