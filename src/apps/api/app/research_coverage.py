"""Summarize original text ranges selected for the initial model prompt."""
from __future__ import annotations


def initial_source_coverage(source_id: str, text_length: int, segments: object) -> dict:
    """Count distinct selected code points; this is not evidence of comprehension."""
    result = {'method': 'initial_prompt_ranges_v1', 'text_characters': text_length,
              'selected_characters': None, 'status': 'unknown'}
    if not isinstance(segments, list) or text_length <= 0:
        return result
    intervals: list[tuple[int, int]] = []
    for item in segments:
        if not isinstance(item, dict) or item.get('source_id') != source_id:
            continue
        start, end = item.get('character_start'), item.get('character_end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= text_length:
            return result
        intervals.append((start, end))
    if not intervals:
        return result
    intervals.sort()
    selected, left, right = 0, *intervals[0]
    for start, end in intervals[1:]:
        if start <= right:
            right = max(right, end)
        else:
            selected += right - left
            left, right = start, end
    selected += right - left
    result.update({'selected_characters': selected, 'status': 'complete' if selected == text_length else 'partial'})
    return result
