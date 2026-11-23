#!/usr/bin/env python3
"""One-shot, offline structural audit of fact-batching results; no app imports or network.

Run only after the batch completes or stops. Emits aggregate and per-run structural
metadata, never reasoning, tool input values, source text, reports or credentials.
"""
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / 'results'
EXPECTED = [f'D{case}-{arm}-{repeat}' for case in range(1, 5)
            for arm in ('A', 'B') for repeat in (1, 2)]


def safe_int(value):
    return value if type(value) is int and value >= 0 else None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def blocks(turn, wire):
    rows = []
    for block in turn.get('blocks', []) if isinstance(turn.get('blocks'), list) else []:
        if not isinstance(block, dict):
            rows.append({'type': 'invalid', 'structure': 'unknown'})
            continue
        kind = block.get('type')
        kind = kind if kind in ('tool_use', 'text', 'thinking', 'redacted_thinking', 'other', 'invalid') else 'other'
        row = {'type': kind, 'chars': safe_int(block.get('chars'))}
        if wire:
            row['block_stop_observed'] = block.get('complete') if type(block.get('complete')) is bool else None
            row['arguments_json_complete'] = block.get('arguments_json_complete') if type(block.get('arguments_json_complete')) is bool else None
        if kind == 'tool_use':
            name = block.get('name')
            row['name'] = name if isinstance(name, str) else None
            shape = block.get('arguments_shape')
            facts = shape.get('facts') if isinstance(shape, dict) else None
            row['facts_items'] = safe_int(facts.get('items')) if isinstance(facts, dict) else None
        rows.append(row)
    return rows


def summarize_turns(data):
    counts = safe_int(data.get('calls'))
    result = {}
    for key, wire in (('wire_turns', True), ('decoded_turns', False)):
        value = data.get(key)
        if not isinstance(value, list):
            result[key] = {'observation': 'not_recorded', 'turns': []}
            continue
        rows = []
        for i, turn in enumerate(value, 1):
            if not isinstance(turn, dict):
                rows.append({'observation_sequence': i, 'structure': 'invalid'})
                continue
            observed = blocks(turn, wire)
            names = [b.get('name') for b in observed if b['type'] == 'tool_use']
            rows.append({'observation_sequence': i,
                         'model_call_ordinal': i if counts == len(value) else None,
                         'finish_reason': turn.get('finish_reason'),
                         'observation_parse_error': turn.get('observation_parse_error') if wire else None,
                         'truncation_position_zero_based': turn.get('truncation_position') if wire else None,
                         'blocks': observed, 'known_tool_names_seen': names,
                         'record_facts_seen': 'yes' if 'record_facts' in names else 'not_seen_in_observed_blocks',
                         'unknown_tool_name_seen': any(n in (None, '<unrecognized>') for n in names)})
        result[key] = {'observation': 'recorded', 'ordinal_mapping': 'exact_count_only_not_identity_verified' if counts == len(value) else 'not_established', 'turns': rows}
    return result


def tool_summary(data, arm):
    io = data.get('tool_io')
    if not isinstance(io, list):
        return {'observation': 'not_recorded', 'instruction_assessment': 'unknown'}
    records = []
    by_turn = defaultdict(list)
    for entry in io:
        if not isinstance(entry, dict) or entry.get('tool') != 'record_facts':
            continue
        args = entry.get('input')
        facts = args.get('facts') if isinstance(args, dict) else None
        turn = safe_int(entry.get('turn'))
        row = {'history_turn': turn, 'tool_call_id': entry.get('id'),
               'submitted_fact_count': len(facts) if isinstance(facts, list) else None,
               'result_present': entry.get('result') is not None,
               '_hashes': [fingerprint(f) for f in facts] if isinstance(facts, list) else None}
        records.append(row)
        by_turn[turn].append(row)
    known_turns = [t for t in by_turn if t is not None]
    parallels = [t for t in sorted(known_turns) if len(by_turn[t]) > 1]
    size_violation = any(r['submitted_fact_count'] is not None and r['submitted_fact_count'] > 2 for r in records)
    unknown_size = any(r['submitted_fact_count'] is None for r in records)
    # Same assistant turn emits both calls before either tool result can enter a later model turn.
    if arm != 'B':
        assessment = 'not_applicable_A'
    elif size_violation or parallels:
        assessment = 'observed_violation'
    elif not records:
        assessment = 'not_tested_no_record_facts_in_history'
    elif unknown_size or None in by_turn or any(not r['result_present'] for r in records[:-1]):
        assessment = 'unknown_missing_shape_turn_or_receipt'
    else:
        assessment = 'structurally_consistent_inspection_not_provable'
    seen = Counter(h for r in records for h in (r['_hashes'] or []))
    identical_input_repeats = sum(n - 1 for n in seen.values() if n > 1)
    for r in records:
        r.pop('_hashes')
    facts = data.get('facts')
    ids = [f.get('fact_id') for f in facts if isinstance(f, dict) and isinstance(f.get('fact_id'), str)] if isinstance(facts, list) else []
    id_counts = Counter(ids)
    return {'observation': 'checkpoint_history', 'history_turns_with_multiple_record_facts': parallels,
            'history_turn_index_missing': None in by_turn, 'calls': records,
            'instruction_assessment': assessment,
            'identical_full_input_fact_repetitions': identical_input_repeats if all(r['submitted_fact_count'] is not None for r in records) else None,
            'saved_fact_count': len(facts) if isinstance(facts, list) else None,
            'saved_duplicate_fact_ids': sum(n-1 for n in id_counts.values() if n>1) if isinstance(facts, list) and len(ids)==len(facts) else None,
            'duplicate_scope': 'exact full fact input object and saved fact IDs only; semantic duplicates require review'}


def analyze(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    parts = path.stem.split('-')
    arm = parts[1] if len(parts) == 3 and parts[1] in ('A', 'B') else None
    usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
    events = data.get('events') if isinstance(data.get('events'), list) else []
    model_events = [e for e in events if isinstance(e, dict) and e.get('kind') in ('model','model_failure')]
    safe_events = [{k: e.get(k) for k in ('kind','call_id','finish_reason','code','external_state','settlement','usage','usage_detail') if k in e} for e in model_events]
    return {'id': path.stem, 'arm': arm, 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'status': data.get('status'), 'reason': data.get('reason'), 'error_type': data.get('error_type'),
            'calls': data.get('calls'), 'elapsed_seconds': data.get('elapsed_seconds'),
            'provider_reported_tokens': safe_int(usage.get('reported_tokens')),
            'unknown_usage_calls': safe_int(usage.get('unknown_usage_calls')),
            'reserved_estimate_tokens': safe_int(usage.get('reserved_estimate_tokens')),
            'budget_accounted_tokens': safe_int(data.get('budget_accounted_tokens')),
            'actual_cost': data.get('actual_cost'), 'model_events': safe_events,
            'observations': summarize_turns(data), 'record_facts': tool_summary(data, arm)}


def main():
    runs, errors = [], []
    for path in sorted(RESULTS.glob('*.json')):
        try:
            runs.append(analyze(path))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append({'id': path.stem, 'error_type': type(exc).__name__})
    by_id = {run['id']: run for run in runs}
    started = sorted(p.stem for p in RESULTS.glob('*.started'))
    groups = {}
    for arm in ('A','B'):
        selected = [r for r in runs if r['arm'] == arm]
        known = [r['provider_reported_tokens'] for r in selected if r['provider_reported_tokens'] is not None]
        groups[arm] = {'result_count': len(selected), 'known_reported_token_sum': sum(known),
                       'reported_usage_missing_runs': len(selected)-len(known),
                       'unknown_usage_calls_known_sum': sum(r['unknown_usage_calls'] for r in selected if r['unknown_usage_calls'] is not None),
                       'unknown_usage_call_count_missing_runs': sum(r['unknown_usage_calls'] is None for r in selected)}
    output = {'expected': EXPECTED, 'observed_result_count': len(runs), 'started_markers': started,
              'missing_result_for_started': sorted(set(started)-set(by_id)),
              'no_started_marker': sorted(set(EXPECTED)-set(started)),
              'unexpected_results': sorted(set(by_id)-set(EXPECTED)), 'read_errors': errors,
              'usage_by_arm': groups, 'runs': runs,
              'limitations': ['No reasoning or input values retained.',
                  'A block not seen in the observed prefix is not proof of absence from the unseen continuation.',
                  'History turn is a checkpoint ordinal; it is not automatically a provider call ordinal.',
                  'A tool result in history does not establish that the model semantically inspected it.',
                  'Duplicate checks are exact, not semantic; actual cost and unknown usage are not estimated.']}
    (HERE / 'mechanism-results.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
    lines = ['# 分批事实记录机制：离线结构审计', '',
             f"预定 {len(EXPECTED)} 轨；结果 {len(runs)}；started 无结果 {len(output['missing_result_for_started'])}；无 started 标记 {len(output['no_started_marker'])}。详见 JSON 的逐轨回执、区分未观测与未开始及输入哈希。", '',
             '| 轨 | 状态/原因 | 调用 | 已保存事实 | record_facts 每次条数（history 轮次） | 同轮多调用 | B 指令结构判读 | reported / budget tokens |',
             '|---|---|---:|---:|---|---|---|---:|']
    for r in runs:
        t = r['record_facts']; calls = t.get('calls', [])
        sizes = ', '.join(f"{c['submitted_fact_count']}（{c['history_turn']}）" for c in calls) or '未见或不可用'
        lines.append(f"| {r['id']} | {r['status']} / {r['reason']} | {r['calls']} | {t.get('saved_fact_count')} | {sizes} | {t.get('history_turns_with_multiple_record_facts')} | {t.get('instruction_assessment')} | {r['provider_reported_tokens']} / {r['budget_accounted_tokens']} |")
    lines += ['', '截断逐轮的已见块类型、长度、工具名、块结束及参数 JSON 完整性见同目录 JSON；未见工具只表示已观测块中未见，不断言未到达的部分不存在。观察轮序只有计数一致时才作暂定对应，须核对回执身份。',
              'B 指令包含每次最多两项及结果返回后再记录：同轮多个 record_facts 或单次大于两项为可见违反；结果回执在后续轮可见仅代表结构相容，不能证明模型理解或检查结果。没有调用不计遵循。',
              '重复计数仅比对完整输入对象和已保存 fact_id，不能排除不同表述的语义重复。每臂 reported 合计和缺失数见 JSON；unknown usage 或费用未知不得视为零。',
              '本审计仅保存结构与长度，不保存思维正文或工具参数值；不执行应用、外呼或模型调用。', '']
    (HERE / 'mechanism-audit.md').write_text('\n'.join(lines))
    print(f"analyzed {len(runs)} result files; {len(errors)} read errors")

if __name__ == '__main__':
    main()
