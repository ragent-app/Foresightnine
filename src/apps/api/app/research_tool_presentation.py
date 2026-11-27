"""Optional, run-scoped presentation of native research tool definitions."""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from hashlib import sha256
from typing import Any, Mapping

from .model_protocol import ModelToolDefinition, canonical_json_dumps
from .research_tools_registry import ALL_TOOL_DEFINITIONS, TOOLS as DEFAULT_TOOLS

ACTIVATE = 'activate_tool_group'
# Each tool retains its normal execution checks. The flags here describe availability
# in the session and do not grant permission to fetch or process any source.
_GROUPS = {
    'presentation': {'create_chart': (), 'create_table': ()},
    'documents': {'read_pages': (), 'extract_statements': (), 'request_ocr': ('pdf_ocr_on_demand',)},
    'market': {'analyze_market_series': (), 'market_data': ('market_data_available', 'cn_market_data_available', 'hk_market_data_available')},
    'disclosures': {
        'company_financials': ('sec_financials_available', 'cn_financials_available'),
        'filings': ('filings_available',),
        'issuer_documents': ('issuer_documents_available',),
        'sec_earnings_release': ('filings_available',),
        'sec_filing_tables': ('filings_available',),
        'sec_filing_sections': (),
        'sec_redline_diff': (),
        'sec_fulltext_search': ('filings_available',),
        'sec_material_events': ('filings_available',),
    },
    'earnings_analysis': {
        'earnings_consensus': ('earnings_consensus_available',),
        'earnings_transcript': ('earnings_transcript_available',),
        'earnings_quality_diagnostics': (),
        'guidance_and_nongaap_reconciler': (),
        'valuation_and_implied_expectations': (),
        'bank_fundamentals_diagnostics': ('sec_financials_available',),
        'bank_valuation_and_peer_compare': ('sec_financials_available',),
    },
    'external_sources': {'acquire_sources': ('acquire_available',), 'news_search': ('news_search_available',)},
}
_DEFERRED = {name for group in _GROUPS.values() for name in group}


def available_groups(state: Mapping[str, Any]) -> dict[str, list[str]]:
    try:
        payload = json.loads(state['user'])
    except (ValueError, TypeError):
        payload = {}
    capabilities = payload.get('available_capabilities') if isinstance(payload, dict) else None
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    groups = {}
    for label, tools in _GROUPS.items():
        names = [name for name, flags in tools.items() if not flags or any(capabilities.get(key) is True for key in flags)]
        if names:
            groups[label] = names
    return groups


def definitions(state: Mapping[str, Any], model: Any) -> tuple[ModelToolDefinition, ...]:
    capacity = getattr(model, 'research_capacity', None)
    profile = capacity() if callable(capacity) else None
    if not (getattr(profile, 'dynamic_tool_definitions', False) and getattr(profile, 'native_tools', False)):
        return DEFAULT_TOOLS
    groups = available_groups(state)
    active = state.get('active_tool_groups', ())
    enabled = {name for group in active if group in groups for name in groups[group]}
    result = []
    for definition in ALL_TOOL_DEFINITIONS:
        if definition.name == ACTIVATE:
            catalog = ', '.join(group + ' (' + ', '.join(names) + ')' for group, names in groups.items())
            result.append(replace(definition, description=definition.description + ' Available groups: ' + catalog))
        elif definition.name not in _DEFERRED or definition.name in enabled:
            result.append(definition)
    return tuple(result)


def definition_metadata(tools: tuple[ModelToolDefinition, ...]) -> tuple[str, int]:
    encoded = canonical_json_dumps([asdict(tool) for tool in tools])
    return sha256(encoded.encode()).hexdigest(), len(encoded)


DEFAULT_METADATA = definition_metadata(DEFAULT_TOOLS)
