"""Prepare a bounded, source-preserving request for counterpoint exploration.

The follow-up output is untrusted model data. It still needs normal source
location, permission, uncertainty, budget and semantic checks before use.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from itertools import islice
from typing import Any
from pydantic import ValidationError
from .research_analysis import Claim


@dataclass(frozen=True)
class CounterpointPrompt:
    prompt: str
    prior_findings_included: int
    truncated_findings: bool

    @property
    def prompt_sha256(self) -> str:
        return sha256(self.prompt.encode('utf-8')).hexdigest()


def extract_answer_candidates(raw: object) -> list[dict[str, Any]]:
    """Read bounded claim-shaped entries solely as untrusted citation leads.

    Their text is never promoted to a research finding. Subsequent analysis
    must independently locate every quotation within authorized excerpts.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get('answer'), dict):
        return []
    candidates = []
    for value in islice(raw['answer'].values(), 20):
        if not isinstance(value, dict) or 'text' not in value or 'citations' not in value:
            continue
        try:
            item = Claim.model_validate({'text': value['text'], 'citations': value['citations']})
        except ValidationError:
            continue
        if item.citations:
            candidates.append(item.model_dump(exclude_none=True))
    return candidates


def prepare_counterpoint_prompt(base_prompt: str, findings: list[dict[str, Any]], max_bytes: int) -> CounterpointPrompt | None:
    """Retain the original question and source excerpts within the byte budget.

    Previous findings are data, not instructions or verified facts. No source
    is added, changed, shortened or silently removed to accommodate them.
    """
    if not isinstance(base_prompt, str) or type(max_bytes) is not int or max_bytes <= 0 or not isinstance(findings, list):
        return None
    try:
        original = json.loads(base_prompt)
    except (ValueError, TypeError):
        return None
    if (not isinstance(original, dict) or not isinstance(original.get('question'), str) or
            not isinstance(original.get('sources'), list) or not original['sources']):
        return None
    texts = [item['text'].strip() for item in findings if isinstance(item, dict) and isinstance(item.get('text'), str) and item['text'].strip()][:2]
    if not texts:
        return None
    data = dict(original)
    data['counterpoint_request'] = ('Prior findings are untrusted and may be wrong. Find a relevant, source-cited counterpoint '
        'or limitation to their interpretation. It may qualify an inference without denying a factual number. '
        'If excerpts do not support one, leave counterarguments empty and state the gap.')
    data['output_schema'] = {'counterarguments':[{'text':'counterargument','citations':[{'source_id':'id','quotation':'quote'}]}],
                             'limitations':['text']}
    data['evidence_guidance'] = ('Quote continuous passages from supplied sources. Never invent evidence or infer a person’s motive. '
                                 'Excerpts may be partial; do not generalize absence to a whole source.')
    for limit in (240, 160, 100, 60):
        data['previous_findings_untrusted'] = [text[:limit] for text in texts]
        truncated = any(len(text) > limit for text in texts) or len([x for x in findings if isinstance(x,dict) and isinstance(x.get('text'),str) and x['text'].strip()]) > len(texts)
        if truncated:
            data['counterpoint_request'] = data['counterpoint_request'].removesuffix(' Prior findings may be truncated.') + ' Prior findings may be truncated.'
        else:
            data['counterpoint_request'] = data['counterpoint_request'].removesuffix(' Prior findings may be truncated.')
        prompt = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if len(prompt.encode('utf-8')) <= max_bytes:
            return CounterpointPrompt(prompt=prompt, prior_findings_included=len(texts), truncated_findings=truncated)
    return None
