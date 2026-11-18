"""Decode model-input v2 for mock providers without altering the wire prompt.

Existing mock providers operate on individual excerpts. Flattening only changes
this local test view; source identity, excerpt text and grouping are preserved.
Tests of budget must continue measuring the original serialized prompt.
"""
import json


def decode_research_prompt(prompt):
    data = json.loads(prompt)
    if not isinstance(data, dict) or data.get('source_input_version') != '2':
        return data
    sources = data.get('sources')
    if not isinstance(sources, list):
        return data
    flattened = []
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get('passages'), list):
            raise AssertionError('Invalid grouped research source in mock input')
        metadata = {key: value for key, value in source.items() if key != 'passages'}
        for index, passage in enumerate(source['passages']):
            if not isinstance(passage, dict) or not isinstance(passage.get('text'), str):
                raise AssertionError('Invalid grouped research passage in mock input')
            identity = {key: value for key, value in metadata.items()
                        if index == 0 or key in {'source_id', 'source_coverage'}}
            flattened.append({**identity, **passage})
    return {**data, 'sources': flattened}


def prompt_budget_with_evidence(question, sources, *, evidence_bytes=600):
    """Measure protocol overhead using tiny placeholders, then allow evidence.

    This is a test-fixture budget only; it never changes production limits.
    """
    from app.research_analysis import analyze
    tiny = [source.model_copy(update={'text': 'X.'}) for source in sources]
    observed = []
    analyze(question, tiny, None, lambda prompt: (observed.append(prompt) or {}, 0), max_prompt_chars=10000)
    if not observed:
        raise AssertionError('Unable to measure the empty evidence protocol')
    return len(observed[0].encode('utf-8')) - 2 * len(tiny) + evidence_bytes
