#!/usr/bin/env python3
"""Summarize frozen manual semantic judgments; never make model calls."""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAN = [(c,a,r) for c in ('D1','D2','D3','D4') for a,r in [('A',1),('B',1),('B',2),('A',2)]]
VALUES = {'supported','unsupported','unknown'}
FIELDS = ('evidence','formal','visible')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pointer(doc, key):
    if key == '': return doc
    if not key.startswith('/'): raise ValueError('JSON pointer must begin with /')
    for token in key[1:].split('/'):
        token = token.replace('~1','/').replace('~0','~')
        doc = doc[int(token)] if isinstance(doc,list) else doc[token]
    return doc


def accepted(run):
    if 'status' not in run or 'reason' not in run or 'submit_handler_receipts' not in run or 'state_submission' not in run or 'tool_io' not in run:
        return 'unknown'
    receipts = run['submit_handler_receipts'] or []
    state = run['state_submission']
    calls = [x for x in run['tool_io'] if x.get('tool') == 'submit_research']
    success = [r for r in receipts if r.get('handler_returned') and (r.get('early_outcome') or {}).get('reason') == 'submitted']
    if run['reason'] != 'submitted' and not state and not success:
        return 'not_accepted'
    if run['reason'] != 'submitted' or not isinstance(state,dict) or len(success) != 1:
        return 'unknown'
    receipt = success[0]
    outcome = receipt['early_outcome']
    if not any(c.get('id') == receipt.get('id') for c in calls): return 'unknown'
    if receipt.get('state_submission') != state or outcome.get('status') != run['status'] or state.get('status') != run['status']:
        return 'unknown'
    if state.get('report') != run.get('report') or state.get('claims') != run.get('claims') or state.get('gaps') != run.get('gaps'):
        return 'unknown'
    if state.get('partial_notes') != run.get('partial_notes'): return 'unknown'
    return 'accepted'


def bounds(values):
    return {'supported':values.count('supported'), 'unknown':values.count('unknown'),
            'total':len(values), 'lower':values.count('supported'),
            'upper':values.count('supported')+values.count('unknown')}


def main():
    if len(sys.argv)!=4:
        raise SystemExit('usage: score.py EXPERIMENT_DIR JUDGMENTS.json OUTPUT.json')
    root, jpath, dest = map(Path,sys.argv[1:])
    root=root.resolve(); judgments=json.loads(jpath.read_text())
    cases={c['id']:c for c in json.loads((root/'cases.json').read_text())['cases']}
    index={x['name']:x for x in json.loads((root/'index.json').read_text())}
    assessments=judgments['runs']; rows=[]; unstarted=[]
    for case,arm,repeat in PLAN:
        name=f'{case}-{arm}-{repeat}'; marker=root/'results'/f'{name}.started'; path=root/'results'/f'{name}.json'
        if not marker.exists():
            unstarted.append(name); continue
        if path.exists() and name in index:
            assert digest(path)==index[name]['sha256'],name
            try: run=json.loads(path.read_text())
            except (ValueError,OSError): run={}
        else: run={}
        if run.get('case') is not None: assert run['case']==case and run['arm']==arm,name
        submission=accepted(run)
        review=assessments.get(name,{})
        if review:
            assert review.get('raw_sha256')==digest(path),name
        docs={'raw':run}; docpaths={'raw':path}
        for label,ref in review.get('documents',{}).items():
            assert label!='raw'
            p=Path(ref['path']); p=p if p.is_absolute() else (jpath.parent/p)
            assert digest(p)==ref['sha256'],(name,label)
            docs[label]=json.loads(p.read_text());docpaths[label]=p
        cores=[]
        for i,criterion in enumerate(cases[case]['core']):
            item=(review.get('core') or [{},{}])[i]; core={'criterion':criterion}
            for field in FIELDS:
                judgment=item.get(field,{'status':'unknown','reason':'未完成逐项核验','refs':[]})
                status=judgment['status']; assert status in VALUES
                assert judgment.get('reason'),(name,i,field)
                refs=judgment.get('refs',[])
                if status=='supported': assert refs,(name,i,field)
                if field=='formal' and status=='supported': assert submission=='accepted',(name,i)
                for ref in refs:
                    assert ref['doc'] in docs and ref.get('excerpt'),(name,i,field)
                    found=pointer(docs[ref['doc']],ref['pointer'])
                    value=found if isinstance(found,str) else json.dumps(found,ensure_ascii=False,sort_keys=True)
                    assert ref['excerpt'] in value,(name,i,field,ref)
                if field=='formal' and status=='supported':
                    assert any(ref['doc']=='raw' and ref['pointer']=='/report' for ref in refs),(name,i,'formal needs accepted report text')
                if field=='visible' and status=='supported':
                    labels={ref['doc'] for ref in refs}
                    assert 'api' in labels and ('ui' in labels or 'export' in labels),(name,i,'visible needs API and UI or export evidence')
                core[field]=judgment
            cores.append(core)
        usage=run.get('usage') or {}
        rows.append(dict(name=name,case=case,arm=arm,repeat=repeat,raw_sha256=digest(path) if path.exists() else None,
                         submission=submission,core=cores,risk_review=review.get('risk_review',{'status':'unknown','reason':'未核验'}),
                         status=run.get('status'),reason=run.get('reason'),calls=run.get('calls'),
                         budget_accounted_tokens=run.get('budget_accounted_tokens'),reported_tokens=usage.get('reported_tokens'),
                         unknown_usage_calls=usage.get('unknown_usage_calls'),reserved_estimate_tokens=usage.get('reserved_estimate_tokens'),seconds=run.get('elapsed_seconds'),actual_cost=run.get('actual_cost')))
    assert set(assessments).issubset({r['name'] for r in rows}),'judgments contain unstarted runs'
    summaries={}
    for arm in 'AB':
        rr=[r for r in rows if r['arm']==arm]
        entry={'started':len(rr),'submission':{s:sum(r['submission']==s for r in rr) for s in ('accepted','not_accepted','unknown')}}
        for field in FIELDS: entry[field]=bounds([c[field]['status'] for r in rr for c in r['core']])
        for field in ('calls','budget_accounted_tokens','reported_tokens','unknown_usage_calls','reserved_estimate_tokens','seconds','actual_cost'):
            values=[r[field] for r in rr]; known=[x for x in values if isinstance(x,(int,float)) and not isinstance(x,bool)]
            entry[field]={'known_sum':sum(known),'unknown_runs':len(values)-len(known)}
        summaries[arm]=entry
    pairs=[]
    for case in ('D1','D2','D3','D4'):
        for repeat in (1,2):
            a=next((r for r in rows if r['case']==case and r['arm']=='A' and r['repeat']==repeat),None)
            b=next((r for r in rows if r['case']==case and r['arm']=='B' and r['repeat']==repeat),None)
            if not a or not b: continue
            result={'case':case,'repeat':repeat}
            for field in FIELDS:
                av=[c[field]['status'] for c in a['core']];bv=[c[field]['status'] for c in b['core']]
                result[field+'_difference']=None if 'unknown' in av+bv else bv.count('supported')-av.count('supported')
            pairs.append(result)
    output={'scoring_protocol_sha256':digest(HERE/'scoring-protocol.md'),'judgments_sha256':digest(jpath),
            'method':'non-blinded manual semantic adjudication; no model; unknown retained',
            'planned':len(PLAN),'started':len(rows),'unstarted':unstarted,'rows':rows,'pairs':pairs,'aggregate':summaries}
    dest.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__': main()
