#!/bin/sh
# Per model request of each 363 run: submission state and transport stages (names and outcomes only).
# Reads the isolated acceptance database; prints no model content, credentials or request bodies.
docker exec foresightnine-acceptance-363-postgres-1 psql -U foresightnine -d foresightnine_acceptance -Atc "
select r.document_key, d.key,
       d.value->'transport'->>'submission_state',
       (select string_agg((s->>'stage') || ':' || (s->>'result'), ',') from jsonb_array_elements(d.value->'transport'->'stages') s)
from research_state_documents r, jsonb_each(r.document->'private_validation_diagnostics') d
where r.collection = 'research_run_inputs'
order by r.document_key, d.key"
