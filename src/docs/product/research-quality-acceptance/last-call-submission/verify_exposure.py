"""Audit executed requests, including a preregistered stop, without external calls."""
import ast
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path


def main():
    base = Path(__file__).resolve().parent
    freeze = json.loads((base / 'experiment-freeze.json').read_text())
    for filename, expected in freeze.items():
        assert sha256((base / filename).read_bytes()).hexdigest() == expected, filename
    tree = ast.parse((base / 'worker.py').read_text())
    instruction = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'INSTRUCTION' for t in node.targets))
    cases = {c['id']: c for c in json.loads((base.parent / 'cases.json').read_text())['cases']}
    first_prompts = defaultdict(set)
    exposure = defaultdict(list)
    plan = [(c, a, r) for c in ('D1', 'D2', 'D3', 'D4') for a, r in (('A',1), ('B',1), ('B',2), ('A',2))]
    unstarted = []
    results = []
    for case, arm, repeat in plan:
        name = f'{case}-{arm}-{repeat}'
        path = base / 'results' / f'{name}.json'
        if not path.exists() and not path.with_suffix('.started').exists():
            unstarted.append(name)
            continue
        assert not unstarted, 'Executed requests must form a prefix of the preregistered order'
        assert path.with_suffix('.started').exists(), name
        data = json.loads(path.read_text())
        results.append(data)
        assert (data['case'], data['arm']) == (case, arm), name
        assert data['source']['content_sha256'] == sha256(cases[case]['snapshot'].encode()).hexdigest(), name
        assert data['calls'] == len(data['prompts']) <= 4 and data['search_calls'] == 0, name
        assert data['budget_accounted_tokens'] <= 160000, name
        first_prompts[case].add(data['prompts'][0]['turn_sha256'])
        for seq, prompt in enumerate(data['prompts'], 1):
            build = next(b for b in data['build_observations'] if b['call_candidate'] == seq)
            expected = arm == 'B' and seq == 4
            assert build['eligible_last_call'] == (seq == 4), name
            assert build['injected'] == prompt['injected'] == expected, name
            assert (instruction in (prompt['tail_note'] or '')) == expected, name
            assert prompt['turn_sha256'] == build['after_sha256'], name
            assert (build['before_sha256'] != build['after_sha256']) == expected, name
            if expected:
                exposure[case].append(name)
        assert (data['prompt_exposure'] == 'provider_entered') == (arm == 'B' and data['calls'] == 4), name
    if unstarted:
        assert any(event.get('settlement') == 'unknown' for event in results[-1]['events']), 'Expected stop receipt'
    assert all(len(hashes) == 1 for hashes in first_prompts.values()), 'Within-case first prompts differ'
    print(json.dumps({'verified_runs': len(results), 'unstarted': unstarted, 'unchanged_first_prompts_by_case': sorted(first_prompts),
                      'B_runs_receiving_final_instruction': dict(exposure)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
