"""Synthetic selector pressure sample; no private evaluation data."""
import json
import time
import tracemalloc
from app.research_evidence_selection import select_evidence_candidates

req = [{'kind': 'contribution:revenue', 'question': 'annual revenue growth drivers'}]
rows = []
for i in range(1000):
    text = 'Revenue 2024 100 and 2025 110.' if i % 4 == 0 else ('Pricing contributed 3 to revenue.' if i % 4 == 1 else 'Market commentary revenue.')
    rows.append({'source_id': str(i), 'start': 0, 'end': len(text), 'text': text + f' sample {i}', 'rank': i})
results = []
for k in (2, 4):
    for profiled in (False, True):
        if profiled:
            tracemalloc.start()
        start = time.perf_counter()
        chosen, info = select_evidence_candidates(rows, req, max_hits=k, max_bytes=512)
        elapsed = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1] if profiled else None
        if profiled:
            tracemalloc.stop()
        results.append({'k': k, 'profiled': profiled, 'seconds': elapsed, 'peak_traced_bytes': peak,
                        'selected_indices': chosen, 'pool_size': info['pool_size'],
                        'shortlist_size': info['shortlist_size'], 'pruned_count': info['pruned_count']})
print(json.dumps(results, indent=2))
# Independent synthetic marked item: 13th attribution-like item is the only designated
# adequate source. This measures pruning, not semantic accuracy or a blind-set result.
marked = [{'source_id': str(i), 'start': 0, 'end': 50,
           'text': ('Revenue 2024 100 2025 110.' if i == 0 else 'Pricing contributed 2 to revenue.'), 'rank': i}
          for i in range(1000)]
_, pruned = select_evidence_candidates(marked, req, max_hits=2)
print(json.dumps({'marked_necessary_index': 999, 'marked_necessary_pruned': 999 >= 12,
                  'shortlist_size': pruned['shortlist_size']}))
small = rows[:9]
beam, _ = select_evidence_candidates(small, req, max_hits=4, beam_width=32)
exhaustive, _ = select_evidence_candidates(small, req, max_hits=4, beam_width=1000)
print(json.dumps({'small_pool': len(small), 'k': 4, 'beam': beam, 'exhaustive': exhaustive,
                  'same_selected': beam == exhaustive, 'exhaustive_beam_width': 1000}))
