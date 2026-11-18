from app.research_source_quality import summarize_source_quality


def test_hash_duplicates_are_recorded_without_independence_claims():
    a = {'source_id': 'a', 'content_sha256': 'a' * 64, 'published_at': '2024-01-01'}
    b = {'source_id': 'b', 'content_sha256': 'a' * 64}
    c = {'source_id': 'c', 'content_sha256': 'b' * 64}
    result = summarize_source_quality([a, b, c, {'source_id': 'legacy'}])
    rows = {row['source_id']: row for row in result['sources']}
    assert rows['a']['same_hash_source_ids'] == ['b']
    assert rows['a']['content_match'] == 'same_hash_as_another'
    assert rows['c']['content_match'] == 'no_same_hash_recorded'
    assert rows['c']['republication_independence'] == 'not_established'
    assert rows['legacy']['content_match'] == 'unknown'
    assert rows['a']['time_fields_recorded']['published_at'] is True
    assert rows['a']['identity_and_time_verification'] == 'not_independently_verified'


def test_structured_query_record_is_not_presented_as_original_document_span():
    from app.research_provenance import derive_evidence_links
    fact = {'fact_id': 'f', 'source_id': 's', 'location': {'character_start': 2, 'character_end': 9},
            'extraction_method': 'sec_company_concept_row_v1',
            'structured_metadata': {'entity_id': 'entity', 'metric_id': 'metric', 'period_end': '2024-12-31'}}
    ref = derive_evidence_links([], [fact], [])['facts'][0]
    assert ref['reference_kind'] == 'structured_query_record' and ref['snapshot_offsets_only'] is True
    assert ref['record_locator']['metric_id'] == 'metric'
    assert ref['semantic_status'] == 'not_independently_verified'
