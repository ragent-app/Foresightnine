from datetime import datetime, timezone
from app.research_evidence import claim_amount_differences, evidence_differences, flag_publication_date_mismatches, flag_rate_level_point_warnings, flag_citation_subject_context, suggest_percentage_evidence, suggest_usd_amount_evidence
from app.research_sources import ResearchSource, SourcePermissions


def fact(fact_id, source_id, value, *, label="Revenue", period="FY2023", unit="USD million", currency="USD"):
    return {"fact_id": fact_id, "source_id": source_id, "value": value,
            "label": label, "period": period, "unit": unit, "currency": currency}


def test_absence_claim_requires_full_source_coverage_before_generalizing():
    from app.research_evidence import flag_partial_source_absence
    text = 'The economy grew. Later, services increased while goods decreased.'
    source = ResearchSource(source_id='s', owner_id='owner', title='Report', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='hash',
        permissions=SourcePermissions(store=True, display=True, model_input=True))
    claim = {'text': 'The source does not provide a breakdown of services and goods.', 'citations': [{'source_id':'s'}]}
    partial = [{'source_id':'s', 'character_start':0, 'character_end':18}]
    assert flag_partial_source_absence([claim], [source], partial)[0]['source_scope_absence_unverified']
    mixed = {**claim, 'text': 'Consumer spending rose. No opposing evidence exists in the sources for goods rising.'}
    assert flag_partial_source_absence([mixed], [source], partial)[0]['source_scope_absence_unverified']
    assert flag_partial_source_absence([{**claim, 'text':'没有反对性证据表明数值应为最终值；相反，资料仍有修订。'}], [source], partial)[0]['source_scope_absence_unverified']
    assert not flag_partial_source_absence([{**claim, 'text':'在这些摘录中没有反对性证据。'}], [source], partial)[0].get('source_scope_absence_unverified')
    assert flag_partial_source_absence([{**claim, 'text':'No opposing evidence is present in the supplied sources.'}], [source], partial)[0]['source_scope_absence_unverified']
    assert not flag_partial_source_absence([{**claim, 'text': 'No opposing evidence exists in the supplied excerpts.'}], [source], partial)[0].get('source_scope_absence_unverified')
    assert flag_partial_source_absence([{**claim, 'text':'该报告没有提供服务和商品的拆分。'}], [source], partial)[0]['source_scope_absence_unverified']
    assert not flag_partial_source_absence([{**claim, 'text':'The provided excerpts do not include a breakdown.'}], [source], partial)[0].get('source_scope_absence_unverified')
    full = [{'source_id':'s', 'character_start':0, 'character_end':18}, {'source_id':'s', 'character_start':18, 'character_end':len(text)}]
    assert not flag_partial_source_absence([claim], [source], full)[0].get('source_scope_absence_unverified')
    gap = [{**full[0], 'character_end':17}, full[1]]
    assert flag_partial_source_absence([claim], [source], gap)[0]['source_scope_absence_unverified']
    assert not flag_partial_source_absence([{**claim, 'authorship':'user'}], [source], partial)[0].get('source_scope_absence_unverified')


def test_plural_source_absence_requires_coverage_of_all_sources_not_only_cited_one():
    from app.research_evidence import flag_partial_source_absence
    first = ResearchSource(source_id='s1', owner_id='owner', title='One', text='First source.', kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='hash',
        permissions=SourcePermissions(store=True, display=True, model_input=True))
    second = first.model_copy(update={'source_id':'s2', 'text':'Second source has more information.'})
    full_first = {'source_id':'s1', 'character_start':0, 'character_end':len(first.text)}
    full_second = {'source_id':'s2', 'character_start':0, 'character_end':len(second.text)}
    claim = {'text':'No opposing evidence exists in the sources for the claim.', 'citations':[{'source_id':'s1'}]}
    assert flag_partial_source_absence([claim], [first, second], [full_first])[0]['source_scope_absence_unverified']
    assert not flag_partial_source_absence([claim], [first, second], [full_first, full_second])[0].get('source_scope_absence_unverified')
    withheld = second.model_copy(update={'permissions':SourcePermissions(store=True, display=True, model_input=False)})
    assert flag_partial_source_absence([claim], [first, withheld], [full_first, full_second])[0]['source_scope_absence_unverified']
    none = {**claim, 'text':'None of the documents mention an opposing view.'}
    assert flag_partial_source_absence([none], [first, second], [full_first])[0]['source_scope_absence_unverified']
    none = {**claim, 'text':'No source disputes that revision.'}
    assert flag_partial_source_absence([none], [first, second], [full_first])[0]['source_scope_absence_unverified']
    none = {**claim, 'text':'没有反对性证据表明数值应为最终值。'}
    assert flag_partial_source_absence([none], [first, second], [full_first])[0]['source_scope_absence_unverified']
    singular = {**claim, 'text':'The source does not mention an opposing view.'}
    assert not flag_partial_source_absence([singular], [first, second], [full_first])[0].get('source_scope_absence_unverified')


def test_causal_wording_requires_located_permitted_quotation_cue():
    from app.research_evidence import flag_uncited_causal_attribution
    text = 'Consumption fell. Costs fell because demand weakened.'
    src = ResearchSource(source_id='s', owner_id='owner', title='Report', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='hash', permissions=SourcePermissions(store=True, display=True))
    def citation(quote):
        start = text.index(quote)
        return {'source_id':'s', 'quotation':quote, 'location':{'character_start':start,'character_end':start+len(quote)}}
    finding = {'text':'Consumption fell, reflecting pandemic effects.', 'citations':[citation('Consumption fell.')]}
    assert flag_uncited_causal_attribution([finding], [src])[0]['causal_attribution_unverified']
    assert 'causal_attribution_unverified' not in finding
    assert flag_uncited_causal_attribution([{**finding, 'text':'消费下降是由于疫情造成的。'}], [src])[0]['causal_attribution_unverified']
    causal = {'text':'Costs fell because demand weakened.', 'citations':[citation('Costs fell because demand weakened.')]}
    assert not flag_uncited_causal_attribution([causal], [src])[0].get('causal_attribution_unverified')
    assert flag_uncited_causal_attribution([causal], [src.model_copy(update={'permissions': SourcePermissions(store=True)})])[0]['causal_attribution_unverified']
    assert flag_uncited_causal_attribution([{**causal, 'citations':[{**causal['citations'][0], 'location':{'status':'ambiguous'}}]}], [src])[0]['causal_attribution_unverified']
    assert not flag_uncited_causal_attribution([{'text':'Consumption fell.', 'causal_attribution_unverified':True}], [src])[0].get('causal_attribution_unverified')


def test_inferred_dissent_motive_is_not_established_by_unrelated_inflation_fact():
    from app.research_evidence import flag_uncited_causal_attribution
    text = ('Inflation compensation remained low. The committee acted because conditions improved. '
            'A member dissented because inflation remained low.')
    src = ResearchSource(source_id='s', owner_id='owner', title='Statement', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='hash', permissions=SourcePermissions(store=True, display=True))
    def cite(quote):
        start = text.index(quote)
        return {'source_id':'s','quotation':quote,'location':{'character_start':start,'character_end':start+len(quote)}}
    claim = {'text':"Low inflation was the implied concern behind the member's dissent.",
             'citations':[cite('Inflation compensation remained low.')]}
    assert flag_uncited_causal_attribution([claim],[src])[0]['causal_attribution_unverified']
    unrelated_cause = {**claim, 'citations':[cite('The committee acted because conditions improved.')]}
    assert flag_uncited_causal_attribution([unrelated_cause],[src])[0]['causal_attribution_unverified']
    direct = {**claim, 'citations':[cite('A member dissented because inflation remained low.')]}
    assert not flag_uncited_causal_attribution([direct],[src])[0].get('causal_attribution_unverified')
    assert flag_uncited_causal_attribution([{**direct, 'citations':[{**direct['citations'][0], 'partial_token':True}]}],[src])[0]['causal_attribution_unverified']


def test_rate_level_percentage_points_are_review_candidates_not_verdicts():
    for text in ("联邦基金利率目标区间维持在4-1/4至4-1/2个百分点。",
                 "利率区间为4.25至4.50个百分点。",
                 "The target range is 4.25 to 4.5 percentage points."):
        original = {"text": text}
        result = flag_rate_level_point_warnings([original])[0]
        assert result["rate_unit_warnings"][0]["status"] == "requires_review"
        assert "rate_unit_warnings" not in original
    for text in ("利率区间为4.25%至4.50%。", "目标区间下调了0.25至0.50个百分点。",
                 "The target range was lowered by 0.25 to 0.50 percentage points.",
                 "利率下调0.25个百分点。"):
        result = flag_rate_level_point_warnings([{"text": text, "rate_unit_warnings": [{"fake": True}]}])[0]
        assert "rate_unit_warnings" not in result


def test_explicit_publication_dates_are_compared_only_with_one_declared_source():
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text="report", kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), published_at=datetime(2023, 10, 27, tzinfo=timezone.utc),
        content_sha256="test", permissions=SourcePermissions(store=True))
    claim = {"text": "BEA 2023年9月发布的公告", "citations": [{"source_id": "s"}]}
    result = flag_publication_date_mismatches([claim], [source])[0]
    assert result["publication_date_warnings"][0]["claimed_text"] == "2023年9月发布"
    assert result["publication_date_warnings"][0]["status"] == "requires_review"
    assert "publication_date_warnings" not in claim
    for text in ("2023年10月发布", "2023年10月27日发布", "released on 2023-10-27", "2023年9月的数据"):
        assert not flag_publication_date_mismatches([{**claim, "text": text}], [source])[0].get("publication_date_warnings")
    for text in ("2023年10月26日发布", "published in 2023-09", "released on 2023-10-26"):
        assert flag_publication_date_mismatches([{**claim, "text": text}], [source])[0]["publication_date_warnings"]
    assert not flag_publication_date_mismatches([{**claim, "citations": [{"source_id": "s"}, {"source_id": "other"}]}], [source])[0].get("publication_date_warnings")
    assert not flag_publication_date_mismatches([claim], [source.model_copy(update={"published_at": None})])[0].get("publication_date_warnings")
    assert not flag_publication_date_mismatches([{**claim, "text": "2023年10月发布", "publication_date_warnings": [{"fake": True}]}], [source])[0].get("publication_date_warnings")


def test_cross_source_differences_are_candidates_not_confirmed_conflicts():
    a = fact("a", "one", "100")
    b = fact("b", "two", "120", label=" revenue ")
    result = evidence_differences([a, b])
    assert len(result) == 1
    assert result[0]["status"] == "requires_entity_and_method_review"
    assert {f["source_id"] for f in result[0]["facts"]} == {"one", "two"}
    assert evidence_differences([a, dict(b, value="100.00")]) == []
    assert evidence_differences([a, dict(b, source_id="one")]) == []
    assert evidence_differences([a, dict(b, period="FY2022")]) == []
    assert evidence_differences([a, dict(b, currency="EUR")]) == []


def test_invalid_or_ambiguous_facts_do_not_create_candidates():
    a, b = fact("a", "one", "100"), fact("b", "two", "120")
    assert evidence_differences([a, b, dict(b, value="130")]) == []
    assert evidence_differences([a, dict(b, value="NaN")]) == []
    assert evidence_differences([a, dict(b, fact_id=[])]) == []


def test_large_group_is_bounded():
    items = [fact(str(i), f"source{i}", str(i)) for i in range(25)]
    candidate = evidence_differences(items)[0]
    assert len(candidate["facts"]) == 20 and candidate["truncated"]


def test_suggests_unique_numeric_evidence_without_claiming_semantic_support():
    annual = {**fact("annual", "annual-source", "2.5", label="Annual GDP", period="2023", unit="percent", currency=None),
              "quotation": "Annual GDP grew 2.5 percent", "location": {"character_start": 0, "character_end": 27},
              "verification": "model_extraction_not_independently_verified"}
    quarter = {**fact("quarter", "quarter-source", "3.2", label="Quarter GDP", period="Q4 2023", unit="percent", currency=None),
               "quotation": "Quarter GDP grew 3.2 percent", "location": {"character_start": 0, "character_end": 28},
               "verification": "model_extraction_not_independently_verified"}
    claim = {"text": "The annual rate is 2.5 percent and the quarter rate is 3.2 percent.",
             "citations": [{"source_id": "quarter-source", "quotation": quarter["quotation"]}]}
    result = suggest_percentage_evidence([claim], [annual, quarter])
    assert result[0]["citations"] == claim["citations"]
    assert result[0]["suggested_fact_evidence"][0]["fact_id"] == "annual"
    assert result[0]["suggested_fact_evidence"][0]["status"] == "requires_semantic_review"
    assert "suggested_fact_evidence" not in claim


def test_suggests_same_source_usd_facts_for_uncited_amounts_without_changing_citations():
    text = 'unit USD millions; revenue 200966; income 83276.'
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    def item(id, value, quote):
        start = text.index(quote)
        return {**fact(id, "s", value, period="FY2025", unit="millions", currency="USD"), "quotation": quote,
                "location": {"character_start": start, "character_end": start + len(quote)},
                "verification": "model_extraction_not_independently_verified"}
    revenue, income = item("revenue", "200966", "revenue 200966"), item("income", "83276", "income 83276")
    claim = {"text": "收入200,966百万美元，营业利润83,276百万美元。",
             "citations": [{"source_id": "s", "quotation": "unit USD millions"}]}
    result = suggest_usd_amount_evidence([claim], [revenue, income], [source])[0]
    assert result["citations"] == claim["citations"]
    assert [x["fact_id"] for x in result["suggested_fact_evidence"]] == ["revenue", "income"]
    assert result["uncited_amounts"] == ["200,966百万美元", "83,276百万美元"]
    assert all(x["status"] == "requires_semantic_review" for x in result["suggested_fact_evidence"])
    assert "uncited_amounts" not in claim
    with_revenue = {**claim, "citations": claim["citations"] + [{"source_id": "s", "quotation": "revenue 200966", "location": revenue["location"]}]}
    result = suggest_usd_amount_evidence([with_revenue], [revenue, income], [source])[0]
    assert result["uncited_amounts"] == ["83,276百万美元"]
    assert [x["fact_id"] for x in result["suggested_fact_evidence"]] == ["income"]
    for changed in ({**income, "authorship": "user"}, {**income, "currency": "EUR"},
                    {**income, "location": {"status": "ambiguous"}}, {**income, "quotation": "invented"}):
        assert not suggest_usd_amount_evidence([claim], [changed], [source])[0].get("uncited_amounts")
    assert not suggest_usd_amount_evidence([claim], [income, {**income, "fact_id": "other"}], [source])[0].get("uncited_amounts")
    assert not suggest_usd_amount_evidence([claim], [income], [source.model_copy(update={"permissions": SourcePermissions(store=True)})])[0].get("uncited_amounts")
    other = source.model_copy(update={"source_id": "other", "text": "200966"})
    mixed = {**claim, "citations": claim["citations"] + [{"source_id": "other", "quotation": "200966"}]}
    assert suggest_usd_amount_evidence([mixed], [revenue], [source, other])[0]["uncited_amounts"] == ["200,966百万美元"]
    cut_text = "revenue 2009660"
    cut_source = source.model_copy(update={"text": cut_text})
    cut_fact = {**revenue, "quotation": "revenue 200966", "location": {"character_start": 0, "character_end": 14}}
    assert not suggest_usd_amount_evidence([claim], [cut_fact], [cut_source])[0].get("uncited_amounts")
    duplicated_source = source.model_copy(update={"text": text + " revenue 200966"})
    assert not suggest_usd_amount_evidence([claim], [revenue], [duplicated_source])[0].get("uncited_amounts")


def test_suggestions_fail_closed_for_ambiguous_or_user_edited_facts():
    base = {**fact("a", "one", "2.5", unit="percent", currency=None), "quotation": "Annual 2.5 percent",
            "location": {"character_start": 0, "character_end": 18}, "verification": "model_extraction_not_independently_verified"}
    claim = {"text": "The rate was 2.5%.", "citations": []}
    assert not suggest_percentage_evidence([claim], [base, {**base, "fact_id": "b", "source_id": "two"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "authorship": "user"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "location": {"status": "ambiguous"}}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "unit": "percentage points"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "currency": "USD"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "quotation": "Missing number!!!"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "location": {"character_start": 0, "character_end": 10}}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([{**claim, "suggested_fact_evidence": [{"fabricated": True}]}], [])[0].get("suggested_fact_evidence")


def test_formula_scale_factor_does_not_suggest_unrelated_percent_fact():
    hundred = {**fact("a", "one", "100", unit="percent", currency=None), "quotation": "Growth 100 percent",
        "location": {"character_start": 0, "character_end": 18}, "verification": "model_extraction_not_independently_verified"}
    formula = {"text": "(120 - 100) / 100 × 100%", "citations": []}
    assert not suggest_percentage_evidence([formula], [hundred])[0].get("suggested_fact_evidence")
    statement = {"text": "Growth was 100%.", "citations": []}
    assert suggest_percentage_evidence([statement], [hundred])[0]["suggested_fact_evidence"][0]["fact_id"] == "a"


def test_unquoted_chinese_percentage_is_flagged_without_claiming_semantic_support():
    claim = {"text": "同比2.6%，不可误读为环比。", "citations": [{"source_id": "one", "quotation": "same month one year Percent 2.0"}]}
    result = suggest_percentage_evidence([claim], [])[0]
    assert result["uncited_percentages"] == ["2.6%"]
    assert "uncited_percentages" not in claim
    assert not suggest_percentage_evidence([{**claim, "citations": [{"quotation": "同比2.6%"}],
                                             "uncited_percentages": ["fabricated"]}], [])[0].get("uncited_percentages")
    assert not suggest_percentage_evidence([{"text": "增长(120-100)/100 × 100%", "citations": []}], [])[0].get("uncited_percentages")


def test_chinese_usd_conversion_flags_only_anchored_unmatched_amounts():
    quote = "2023 revenue 53717, 2024 revenue 57063 million USD"
    def sourced(id, value, year):
        return {**fact(id, "one", value, period=year, unit="USD millions"), "quotation": quote,
                "location": {"character_start": 0, "character_end": len(quote)},
                "verification": "model_extraction_not_independently_verified"}
    facts = [sourced("prior", "53717", "2023"), sourced("current", "57063", "2024")]
    claim = {"text": "2023为53,717百万美元，2024为57,063百万美元，即57,070.63亿美元。",
             "citations": [{"source_id": "one"}]}
    warning = claim_amount_differences([claim], facts)
    assert len(warning) == 1 and warning[0]["claimed_amount"] == "57,070.63亿美元"
    assert warning[0]["status"] == "requires_review" and warning[0]["source_fact_ids"] == ["prior", "current"]
    for amount in ("570.63亿美元", "570.6亿美元", "57063百万美元"):
        assert claim_amount_differences([{**claim, "text": f"2024为57,063百万美元，即{amount}。"}], facts) == []
    assert claim_amount_differences([{**claim, "text": "金额57,070.63亿美元。"}], facts) == []
    assert claim_amount_differences([{**claim, "citations": [{"source_id": "other"}]}], facts) == []
    assert claim_amount_differences([claim], [{**f, "authorship": "user"} for f in facts]) == []


def test_citation_subject_context_is_an_explicit_bounded_review_hint():
    text = "Personal saving rate was 2.7 percent in the third quarter, a downward revision of 0.1 percentage point."
    quote = "quarter, a downward revision of 0.1 percentage point."
    start = text.index(quote)
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="hash",
        permissions=SourcePermissions(store=True, display=True, model_input=True))
    claim = {"text": "GDP was revised down", "citations": [{"source_id": "s", "quotation": quote,
        "location": {"character_start": start, "character_end": start + len(quote)}}]}
    result = flag_citation_subject_context([claim], [source])[0]
    assert result["citation_context_warnings"] == [{"source_id": "s", "citation_index": 0,
        "context_metric": "personal saving rate", "context_character_start": 0,
        "context_character_end": len("Personal saving rate"), "status": "requires_review"}]
    assert "citation_context_warnings" not in claim
    full = {**claim, "citations": [{"source_id": "s", "quotation": text,
        "location": {"character_start": 0, "character_end": len(text)}}], "citation_context_warnings": [{"fake": True}]}
    assert "citation_context_warnings" not in flag_citation_subject_context([full], [source])[0]
    denied = source.model_copy(update={"permissions": SourcePermissions(store=True, display=False)})
    assert not flag_citation_subject_context([claim], [denied])[0].get("citation_context_warnings")
    bad = {**claim, "citations": [{**claim["citations"][0], "location": {"status": "ambiguous"}}]}
    assert not flag_citation_subject_context([bad], [source])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([{**claim, "citations": [{"source_id": [], "quotation": quote}]}], [source])[0].get("citation_context_warnings")


def test_fractional_percentage_is_not_misread_as_its_denominator():
    supported = {"text": "目标区间为2至2-1/4 percent。", "citations": [{"quotation": "target range 2 to 2-1/4 percent"}]}
    assert not suggest_percentage_evidence([supported], [])[0].get("uncited_percentages")
    unsupported = {**supported, "citations": [{"quotation": "target range 2 to 2-1/2 percent"}]}
    assert suggest_percentage_evidence([unsupported], [])[0]["uncited_percentages"] == ["2-1/4 percent"]
    fraction = {"text": "下调1/4 percent。", "citations": [{"quotation": "lowered by 0.25 percent"}]}
    assert not suggest_percentage_evidence([fraction], [])[0].get("uncited_percentages")
    inexact = {"text": "增加1/3 percent。", "citations": [{"quotation": "increase 1/3 percent"}]}
    assert suggest_percentage_evidence([inexact], [])[0]["uncited_percentages"] == ["1/3 percent"]


def test_table_row_warning_requires_header_context_unless_header_and_unit_are_cited():
    text = ("ACME Corp\nConsolidated Statements of Operations\n(in millions)\n"
            "| Years Ended | 2024 | 2023 |\n| Product | 80 | 90 |\n| Total revenue | 100 | 110 |\n")
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="hash",
        permissions=SourcePermissions(store=True, display=True, model_input=True))
    def citation(quote):
        start = text.index(quote)
        return {"source_id": "s", "quotation": quote, "location": {"character_start": start, "character_end": start + len(quote)}}
    row = citation("| Total revenue | 100 | 110 |")
    claim = {"text": "Revenue was 100 million in 2024", "citations": [row]}
    result = flag_citation_subject_context([claim], [source])[0]
    warning = result["citation_context_warnings"][0]
    assert warning["context_metric"] == "financial table header"
    assert "Years Ended" in text[warning["context_character_start"]:warning["context_character_end"]]
    assert "millions" in text[warning["context_character_start"]:warning["context_character_end"]]
    header, unit = citation("| Years Ended | 2024 | 2023 |"), citation("(in millions)")
    assert not flag_citation_subject_context([{**claim, "citations": [row, header, unit]}], [source])[0].get("citation_context_warnings")
    assert flag_citation_subject_context([{**claim, "citations": [row, header]}], [source])[0].get("citation_context_warnings")
    separated = source.model_copy(update={"text": text.replace("| Product |", "Other narrative\n| Product |")})
    moved = separated.text.index(row["quotation"])
    changed = {**claim, "citations": [{**row, "location": {"character_start": moved, "character_end": moved + len(row["quotation"])}}]}
    assert not flag_citation_subject_context([changed], [separated])[0].get("citation_context_warnings")


def test_truncated_conflict_keeps_different_sources_and_values_visible():
    first = [fact(str(i), "source-a", "100") for i in range(20)]
    case_one = first + [fact("different", "source-b", "110")]
    # Here the differing value belongs to the original source; the only
    # other source matches the first value and appears beyond the cap.
    case_two = first + [fact("other-source", "source-b", "100"), fact("different", "source-a", "110")]
    for case in (case_one, case_two):
        candidate = evidence_differences(case)[0]
        assert candidate["truncated"] and len(candidate["facts"]) == 20
        assert any(a["source_id"] != b["source_id"] and a["value"] != b["value"]
                   for a in candidate["facts"] for b in candidate["facts"])
        assert candidate["status"] == "requires_entity_and_method_review"


def test_single_chinese_amount_with_same_number_but_wrong_billion_scale_is_flagged():
    quote = "Personal income 74.4"
    item = {**fact("f", "one", "74.4", unit="billions of dollars", currency="USD"),
            "quotation": quote, "location": {"character_start": 0, "character_end": len(quote)},
            "verification": "model_extraction_not_independently_verified"}
    claim = {"text": "个人收入变化为74.4亿美元。", "citations": [{"source_id": "one"}]}
    result = claim_amount_differences([claim], [item])
    assert result == [{"claim_index": 0, "claimed_amount": "74.4亿美元", "source_fact_ids": ["f"], "status": "requires_review"}]
    assert not claim_amount_differences([{**claim, "text": "个人收入变化为744亿美元。"}], [item])
    assert not claim_amount_differences([{**claim, "text": "个人收入变化为74.4个十亿美元。"}], [item])
    paired = claim_amount_differences([{**claim, "text": "个人收入变化为74.4亿美元，即74.4个十亿美元。"}], [item])
    assert [x["claimed_amount"] for x in paired] == ["74.4亿美元"]
    assert not claim_amount_differences([{**claim, "text": "个人收入变化为75亿美元。"}], [item])
    assert not claim_amount_differences([claim], [item, {**item, "fact_id": "other", "value": "74.4"}])
    assert not claim_amount_differences([claim], [{**item, "currency": "EUR"}])


def test_suggest_uncited_numeric_facts_stays_with_cited_unique_sources():
    from app.research_evidence import suggest_uncited_numeric_facts
    def make_source(sid, value):
        text = f"September revision. Personal income {value}."
        src = ResearchSource(source_id=sid, owner_id="owner", title="Release", text=text, kind="user_supplied",
            obtained_at=datetime.now(timezone.utc), content_sha256="hash", permissions=SourcePermissions(store=True, display=True))
        quote = f"Personal income {value}"; start = text.index(quote)
        item = {**fact(sid, sid, value, unit="billions of dollars", currency="USD"), "quotation": quote,
                "location": {"character_start": start, "character_end": start + len(quote)},
                "verification": "model_extraction_not_independently_verified"}
        heading = {"source_id": sid, "quotation": "September revision", "location": {"character_start": 0, "character_end": 18}}
        return src, item, heading
    a, fa, ca = make_source("a", "74.4")
    b, fb, cb = make_source("b", "88.2")
    claim = {"text": "从74.4修订为88.2。", "citations": [ca, cb]}
    result = suggest_uncited_numeric_facts([claim], [fa, fb], [a, b])[0]
    assert len(result["uncited_fact_values"]) == 2
    assert {x["fact_id"] for x in result["suggested_fact_evidence"]} == {"a", "b"}
    assert all(x["status"] == "requires_semantic_review" for x in result["suggested_fact_evidence"])
    assert "uncited_fact_values" not in claim
    covered = {**claim, "citations": [ca, cb, {"source_id": "a", "quotation": fa["quotation"], "location": fa["location"]}]}
    assert len(suggest_uncited_numeric_facts([covered], [fa, fb], [a, b])[0]["uncited_fact_values"]) == 1
    assert not suggest_uncited_numeric_facts([{**claim, "citations": []}], [fa, fb], [a, b])[0].get("uncited_fact_values")
    assert not suggest_uncited_numeric_facts([claim], [{**fa, "authorship": "user"}, {**fb, "authorship": "user"}], [a, b])[0].get("uncited_fact_values")
    assert not suggest_uncited_numeric_facts([claim], [fa, {**fa, "fact_id": "duplicate"}], [a, b])[0].get("uncited_fact_values")
    assert not suggest_uncited_numeric_facts([claim], [fa], [a.model_copy(update={"permissions": SourcePermissions(store=True)})])[0].get("uncited_fact_values")


def test_revision_candidates_keep_original_label_and_publication_metadata():
    earlier = fact("old", "one", "74.4", label="2024年9月个人收入环比变化（10月公告初值）",
                   period="2024年9月（较8月变化）；发布于2024-11-27", unit="billions", currency="USD")
    later = fact("new", "two", "88.2", label="2024年9月个人收入环比变化（11月公告修订值）",
                 period="2024年9月（较8月变化）；发布于2024-12-20", unit="billions", currency="USD")
    result = evidence_differences([earlier, later])[0]
    assert result["status"] == "requires_entity_and_method_review"
    assert result["matching_basis"] == "release_qualifiers_ignored"
    assert result["label"] == "2024年9月个人收入环比变化" and result["period"] == "2024年9月（较8月变化）"
    assert [x["period"] for x in result["facts"]] == [earlier["period"], later["period"]]
    assert [x["label"] for x in result["facts"]] == [earlier["label"], later["label"]]
    assert not evidence_differences([earlier, {**later, "label": "2024年9月个人收入环比变化（实际值）"}])
    assert not evidence_differences([earlier, {**later, "period": "2024年10月（较9月变化）；发布于2024-12-20"}])
    assert not evidence_differences([earlier, {**later, "unit": "millions"}])


def test_release_dates_in_labels_preserve_cross_source_revision_candidate():
    earlier = fact('old', 'one', '94.0', label='2023年8月个人收入较前月变化（2023-10-27发布，修订值）',
                   period='2023年8月', unit='billions of dollars, seasonally adjusted at annual rates', currency='USD')
    later = fact('new', 'two', '105.1', label='2023年8月个人收入较前月变化（2023-11-30发布，修订值）',
                 period='2023年8月', unit='billions of dollars, seasonally adjusted at annual rates', currency='USD')
    candidate = evidence_differences([earlier, later])[0]
    assert candidate['status'] == 'requires_entity_and_method_review'
    assert candidate['matching_basis'] == 'release_qualifiers_ignored'
    assert [entry['label'] for entry in candidate['facts']] == [earlier['label'], later['label']]
    assert not evidence_differences([earlier, {**later, 'label':'2023年8月个人收入较前月变化（2023-11-30發布，实际值）'}])
    assert not evidence_differences([earlier, {**later, 'label':'2023年8月个人收入较前月变化（2023-99-99发布，修订值）'}])
    assert not evidence_differences([earlier, {**later, 'period':'2023年9月'}])


def test_cross_source_comparison_suggests_only_eligible_located_fact_without_attaching_it():
    from app.research_evidence import suggest_cross_source_comparison_evidence
    def make(sid, value):
        quote = 'Personal income ' + value
        source = ResearchSource(source_id=sid, owner_id='owner', title='Release', text=quote, kind='user_supplied',
            obtained_at=datetime.now(timezone.utc), content_sha256='hash',
            permissions=SourcePermissions(store=True, display=True, model_input=True))
        item = {**fact(sid, sid, value, label='Personal income', period='2023-08', unit='billions', currency='USD'),
                'quotation':quote, 'location':{'character_start':0,'character_end':len(quote)},
                'verification':'model_extraction_not_independently_verified'}
        return source,item
    a,fa = make('a','94.0'); b,fb = make('b','105.1')
    citation = {'source_id':'b','quotation':fb['quotation'],'location':fb['location']}
    claim = {'text':'The estimate was revised from 94.0 to 105.1.', 'citations':[citation]}
    def run(claims=None, facts=None, sources=None):
        return suggest_cross_source_comparison_evidence(claims or [claim], facts or [fa,fb], sources or [a,b])[0]
    result = run()
    assert result['citations'] == [citation] and result['uncited_fact_values'] == ['94.0 billions']
    assert [item['fact_id'] for item in result['suggested_fact_evidence']] == ['a']
    assert result['suggested_fact_evidence'][0]['status'] == 'requires_semantic_review'
    assert 'suggested_fact_evidence' not in claim
    assert not run(claims=[{**claim,'text':'The estimate is 105.1.'}]).get('suggested_fact_evidence')
    assert not run(claims=[{**claim,'citations':[]}]).get('suggested_fact_evidence')
    assert not run(facts=[{**fa,'authorship':'user'},fb]).get('suggested_fact_evidence')
    assert not run(facts=[{**fa,'quotation':'invented'},fb]).get('suggested_fact_evidence')
    assert not run(facts=[fa,{**fb,'period':'2023-09'}]).get('suggested_fact_evidence')
    assert not run(sources=[a.model_copy(update={'permissions':SourcePermissions(store=True,display=True)}),b]).get('suggested_fact_evidence')
    assert not run(sources=[a.model_copy(update={'is_simulated':True}),b]).get('suggested_fact_evidence')
    c,fc = make('c','94.0')
    assert not run(facts=[fa,fb,fc], sources=[a,b,c]).get('suggested_fact_evidence')


def test_unfinished_quoted_phrase_is_flagged_without_claiming_it_is_false():
    text = "GDP revisions included consumer spending and nonresidential fixed investment."
    quote = "consumer spending and nonresidential"
    start = text.index(quote)
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="hash", permissions=SourcePermissions(store=True, display=True))
    claim = {"text": "Investment was revised", "citations": [{"source_id": "s", "quotation": quote,
        "location": {"character_start": start, "character_end": start + len(quote)}}]}
    warning = flag_citation_subject_context([claim], [source])[0]["citation_context_warnings"][0]
    assert warning["context_metric"] == "unfinished quoted phrase" and warning["status"] == "requires_review"
    assert "fixed investment" in text[start:warning["context_character_end"]]
    complete = {**claim, "citations": [{"source_id": "s", "quotation": "nonresidential fixed investment.",
        "location": {"character_start": text.index("nonresidential"), "character_end": len(text)}}]}
    assert all(item["context_metric"] != "unfinished quoted phrase" for item in
               flag_citation_subject_context([complete], [source])[0].get("citation_context_warnings", []))


def test_fractional_rate_levels_are_not_percentage_point_changes():
    for text in ('目标利率区间上调至 1/4 至 1/2 个百分点。',
                 '目标区间为1-1/4至1-1/2个百分点。',
                 'The target range is 1/4 to 1/2 percentage points.'):
        assert flag_rate_level_point_warnings([{'text': text}])[0]['rate_unit_warnings']
    for text in ('目标区间上调了1/4至1/2个百分点。', '目标区间为1/4至1/2%。'):
        assert not flag_rate_level_point_warnings([{'text': text}])[0].get('rate_unit_warnings')


def test_basis_points_require_equivalent_explicit_quotation():
    from app.research_evidence import flag_uncited_basis_points
    item = {'text': '政策加息25个基点。', 'citations': [{'quotation': 'ongoing increases will be appropriate'}]}
    assert flag_uncited_basis_points([item])[0]['uncited_basis_points'] == ['25个基点']
    for quote in ('raised by 25 basis points', 'raised by 0.25 percentage point', 'raised by 1/4 percentage point', '增加25个基点'):
        assert not flag_uncited_basis_points([{**item, 'citations': [{'quotation': quote}]}])[0].get('uncited_basis_points')
    for quote in ('target range is 0.25 percent', 'raised by 0.5 percentage point', 'rates changed in 2025'):
        assert flag_uncited_basis_points([{**item, 'citations': [{'quotation': quote}]}])[0]['uncited_basis_points'] == ['25个基点']
    stale = {**item, 'text': 'No rate change stated', 'uncited_basis_points': ['old']}
    assert not flag_uncited_basis_points([stale])[0].get('uncited_basis_points')


def test_traditional_chinese_basis_points_and_rate_levels_are_checked():
    from app.research_evidence import flag_uncited_basis_points
    item = {'text': '利率上調75個基點。', 'citations': [{'quotation': 'ongoing increases are appropriate'}]}
    assert flag_uncited_basis_points([item])[0]['uncited_basis_points'] == ['75個基點']
    item['citations'] = [{'quotation': '利率上調0.75個百分點'}]
    assert not flag_uncited_basis_points([item])[0].get('uncited_basis_points')
    level = {'text': '目標利率區間上調至3-3/4至4個百分點。'}
    assert flag_rate_level_point_warnings([level])[0]['rate_unit_warnings']
    change = {'text': '目標區間上調了1/4至1/2個百分點。'}
    assert not flag_rate_level_point_warnings([change])[0].get('rate_unit_warnings')


def test_fraction_denominator_is_not_suggested_as_unrelated_numeric_fact():
    from app.research_evidence import suggest_uncited_numeric_facts
    text = 'Inflation target was 2 percent. Target range was 5-1/4 to 5-1/2 percent.'
    src = ResearchSource(source_id='policy', owner_id='owner', title='Statement', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='hash', permissions=SourcePermissions(store=True, display=True))
    quote = 'Inflation target was 2 percent'; start = text.index(quote)
    item = {**fact('inflation', 'policy', '2', unit='percent'), 'quotation': quote,
            'location': {'character_start': start, 'character_end': start + len(quote)},
            'verification': 'model_extraction_not_independently_verified'}
    range_quote = 'Target range was 5-1/4 to 5-1/2 percent'; a = text.index(range_quote)
    claim = {'text': '目标维持在5-1/4%至5-1/2%。', 'citations': [{'source_id': 'policy', 'quotation': range_quote,
             'location': {'character_start': a, 'character_end': a + len(range_quote)}}]}
    result = suggest_uncited_numeric_facts([claim], [item], [src])[0]
    assert not result.get('uncited_fact_values') and not result.get('suggested_fact_evidence')


def test_relative_clause_flags_omitted_subject_even_when_quote_mentions_another_metric():
    text = 'Other information. Imports, which are a subtraction in the calculation of GDP, were revised down.'
    quote = 'which are a subtraction in the calculation of GDP, were revised down.'
    start = text.index(quote)
    source = ResearchSource(source_id='s', owner_id='owner', title='Report', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='hash', permissions=SourcePermissions(store=True, display=True, model_input=True))
    claim = {'text':'Inventory was subtracted from GDP.', 'citations':[{'source_id':'s', 'quotation':quote,
        'location':{'character_start':start, 'character_end':start+len(quote)}}]}
    flagged = flag_citation_subject_context([claim], [source])[0]
    warning = flagged['citation_context_warnings'][0]
    assert warning['context_metric'] == 'relative-clause antecedent'
    assert 'Imports,' in text[warning['context_character_start']:warning['context_character_end']]
    assert warning['status'] == 'requires_review' and not claim.get('citation_context_warnings')
    full = 'Imports, ' + quote; full_start = text.index(full)
    complete = {**claim, 'citations':[{'source_id':'s', 'quotation':full,
        'location':{'character_start':full_start,'character_end':full_start+len(full)}}]}
    assert not flag_citation_subject_context([complete], [source])[0].get('citation_context_warnings')
    denied = source.model_copy(update={'permissions':SourcePermissions(store=True, display=False)})
    assert not flag_citation_subject_context([claim], [denied])[0].get('citation_context_warnings')
    assert not flag_citation_subject_context([{**claim, 'citations':[{**claim['citations'][0], 'location':{'status':'ambiguous'}}]}], [source])[0].get('citation_context_warnings')


def test_table_header_warning_handles_separate_date_and_unit_rows_and_row_fragments():
    text = ('| [colspan=3] | [colspan=9] Year Ended |\n'
            '| [colspan=3] | January 29, 2023 | January 30, 2022 |\n'
            '| [colspan=3] | (In millions) |\n'
            '| Net cash provided by operating activities | $ | 5,641 | $ | 9,108 |\n')
    source = ResearchSource(source_id='s', owner_id='owner', title='Report', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='hash',
        permissions=SourcePermissions(store=True, display=True, model_input=True))
    def citation(quote):
        start = text.index(quote)
        return {'source_id': 's', 'quotation': quote, 'location': {'character_start': start, 'character_end': start + len(quote)}}
    row = citation('Net cash provided by operating activities | $ | 5,641')
    claim = {'text': 'Cash flow was 5,641 million in 2023', 'citations': [row]}
    warning = flag_citation_subject_context([claim], [source])[0]['citation_context_warnings'][0]
    context = text[warning['context_character_start']:warning['context_character_end']]
    assert warning['context_metric'] == 'financial table header'
    assert all(value in context for value in ('Year Ended', '2023', '2022', 'millions'))
    header = citation(text[:text.index('| [colspan=3] | (In millions)')])
    unit = citation('(In millions)')
    assert not flag_citation_subject_context([{**claim, 'citations': [row, header, unit]}], [source])[0].get('citation_context_warnings')
    separated = source.model_copy(update={'text': text.replace('| [colspan=3] | (In millions) |', 'Narrative break')})
    moved = separated.text.index(row['quotation'])
    changed = {**claim, 'citations': [{**row, 'location': {'character_start': moved, 'character_end': moved + len(row['quotation'])}}]}
    assert not flag_citation_subject_context([changed], [separated])[0].get('citation_context_warnings')


def test_uncited_usd_amounts_are_checked_without_extracted_facts():
    from app.research_evidence import flag_uncited_usd_amounts
    text = "Purchases continue. USD 80 billion and 40 billion U.S. dollars."
    source = ResearchSource(source_id="s", owner_id="owner", title="Statement", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    quote = "Purchases continue."
    citation = {"source_id": "s", "quotation": quote, "location": {"character_start": 0, "character_end": len(quote)}}
    claim = {"text": "每月购买800亿美元及400亿美元。", "citations": [citation]}
    result = flag_uncited_usd_amounts([claim], [source])[0]
    assert result["uncited_claim_amounts"] == ["800亿美元", "400亿美元"]
    assert "uncited_claim_amounts" not in claim
    quote = "USD 80 billion and 40 billion U.S. dollars"
    start = text.index(quote)
    located = {"source_id": "s", "quotation": quote, "location": {"character_start": start, "character_end": start + len(quote)}}
    assert not flag_uncited_usd_amounts([{**result, "citations": [located]}], [source])[0].get("uncited_claim_amounts")
    for changed in ({**located, "partial_token": True}, {**located, "location": {**located["location"], "status": "ambiguous"}}, {**located, "quotation": "USD 80 billion"}):
        assert len(flag_uncited_usd_amounts([{**claim, "citations": [changed]}], [source])[0]["uncited_claim_amounts"]) == 2
    assert len(flag_uncited_usd_amounts([{**claim, "citations": [located]}], [source.model_copy(update={"permissions": SourcePermissions(store=True)})])[0]["uncited_claim_amounts"]) == 2


def test_usd_amount_comparison_is_exact_and_dollar_sign_alone_is_ambiguous():
    from app.research_evidence import flag_uncited_usd_amounts
    text = "$80 billion; USD 80.01 billion."
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    citation = {"source_id": "s", "quotation": text, "location": {"character_start": 0, "character_end": len(text)}}
    result = flag_uncited_usd_amounts([{"text": "800亿美元。", "citations": [citation]}], [source])[0]
    assert result["uncited_claim_amounts"] == ["800亿美元"]


def test_percentage_points_require_quotation_or_exact_basis_point_equivalent():
    from app.research_evidence import flag_uncited_percentage_points
    item = {"text": "净上修约0.2个百分点。", "citations": [{"quotation": "Revision was upward."}]}
    assert flag_uncited_percentage_points([item])[0]["uncited_percentage_points"] == ["0.2个百分点"]
    for quote in ("0.2 percentage points", "20 basis points", "20个基点"):
        assert not flag_uncited_percentage_points([{**item, "citations": [{"quotation": quote}]}])[0].get("uncited_percentage_points")
    for quote in ("0.2 percent", "2 basis points", "increased 0.2"):
        assert flag_uncited_percentage_points([{**item, "citations": [{"quotation": quote}]}])[0]["uncited_percentage_points"]
    assert not flag_uncited_percentage_points([{**item, "text": "No amount", "uncited_percentage_points": ["stale"]}])[0].get("uncited_percentage_points")


def test_related_next_sentence_is_a_review_candidate_and_clears_when_cited():
    from app.research_evidence import flag_citation_subject_context
    text = "Officials may adjust policy. They monitor labor market conditions and inflation pressures."
    source = ResearchSource(source_id="s", owner_id="owner", title="Statement", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    first = "Officials may adjust policy."
    second = "They monitor labor market conditions and inflation pressures."
    def cite(quote):
        start = text.index(quote)
        return {"source_id":"s", "quotation":quote, "location":{"character_start":start,"character_end":start+len(quote)}}
    claim = {"text":"Officials may adjust policy and monitor labor market conditions.", "citations":[cite(first)]}
    result = flag_citation_subject_context([claim], [source])[0]
    cue = result["citation_context_warnings"][0]
    assert cue["context_metric"] == "related unquoted sentence"
    assert text[cue["context_character_start"]:cue["context_character_end"]] == second
    assert cue["status"] == "requires_review"
    assert "citation_context_warnings" not in claim
    assert not flag_citation_subject_context([{**claim,"citations":[cite(first),cite(second)]}], [source])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([claim], [source.model_copy(update={"permissions": SourcePermissions(store=True)})])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([{**claim,"citations":[{**cite(first),"partial_token":True}]}], [source])[0].get("citation_context_warnings")


def test_related_sentence_detection_does_not_assume_unrelated_or_ambiguous_content():
    from app.research_evidence import flag_citation_subject_context
    text = "Policy was maintained. Another unrelated sentence follows."
    source = ResearchSource(source_id="s", owner_id="owner", title="Statement", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    quote = "Policy was maintained."
    citation = {"source_id":"s", "quotation":quote, "location":{"character_start":0,"character_end":len(quote)}}
    claim = {"text":"Policy remained unchanged.", "citations":[citation]}
    assert not flag_citation_subject_context([claim], [source])[0].get("citation_context_warnings")


def test_pronoun_quotation_exposes_same_sentence_antecedent_without_claiming_support():
    from app.research_evidence import flag_citation_subject_context
    text = "Last December, the Committee indicated that it would continue to buy securities."
    source = ResearchSource(source_id="s", owner_id="owner", title="Statement", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    def citation(quote):
        start = text.index(quote)
        return {"source_id":"s", "quotation":quote, "location":{"character_start":start,"character_end":start+len(quote)}}
    original = {"text":"The Committee would continue to buy securities.", "citations":[citation("it would continue to buy securities.")]}
    cue = flag_citation_subject_context([original],[source])[0]["citation_context_warnings"][0]
    assert cue["context_metric"] == "personal-pronoun antecedent"
    assert "Last December, the Committee indicated that" in text[cue["context_character_start"]:cue["context_character_end"]]
    assert cue["status"] == "requires_review"
    covered = {**original, "citations":[citation("Last December, the Committee indicated that"),*original["citations"]]}
    assert not flag_citation_subject_context([covered],[source])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([original],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0].get("citation_context_warnings")


def test_amount_starting_quote_exposes_its_preceding_subject_and_period():
    from app.research_evidence import flag_citation_subject_context
    text = "The total deficit was $68.9 billion in February, up $1.3 billion from $67.6 billion in January, revised."
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    def cite(quote):
        start = text.index(quote)
        return {"source_id":"s", "quotation":quote, "location":{"character_start":start,"character_end":start+len(quote)}}
    claim = {"text":"The January deficit was $67.6 billion.", "citations":[cite("$67.6 billion in January, revised.")]}
    cue = flag_citation_subject_context([claim],[source])[0]["citation_context_warnings"][0]
    assert cue["context_metric"] == "numeric quotation subject"
    assert "total deficit" in text[cue["context_character_start"]:cue["context_character_end"]]
    assert cue["status"] == "requires_review"
    assert not flag_citation_subject_context([{**claim, "citations":[cite(text)]}],[source])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([claim],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0].get("citation_context_warnings")


def test_dollar_amounts_without_structured_facts_require_located_quotations():
    from app.research_evidence import flag_uncited_dollar_amounts
    text = "Goods deficit narrowed $0.3 billion. Exports increased $5.8 billion; imports rose $7.1 billion."
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    def cite(quote):
        start=text.index(quote)
        return {"source_id":"s","quotation":quote,"location":{"character_start":start,"character_end":start+len(quote)}}
    claim={"text":"Imports rose $7.1 billion versus $5.8 billion for exports.", "citations":[cite("Goods deficit narrowed $0.3 billion.")]}
    flagged=flag_uncited_dollar_amounts([claim],[source])[0]
    assert flagged["uncited_dollar_amounts"]==["$7.1 billion", "$5.8 billion"]
    assert "uncited_dollar_amounts" not in claim
    added={**claim,"citations":claim["citations"]+[cite("Exports increased $5.8 billion; imports rose $7.1 billion.")]}
    assert not flag_uncited_dollar_amounts([added],[source])[0].get("uncited_dollar_amounts")
    for change in ({"partial_token":True},{"location":{"status":"ambiguous"}}, {"quotation":"$7.1 billion"}):
        assert flag_uncited_dollar_amounts([{**claim,"citations":[{**added["citations"][-1],**change}]}],[source])[0]["uncited_dollar_amounts"]
    assert flag_uncited_dollar_amounts([added],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0]["uncited_dollar_amounts"]


def test_dollar_amount_normalization_compares_scale_and_sign_not_currency():
    from app.research_evidence import flag_uncited_dollar_amounts
    text="A $7,100 million rise and -$2 billion loss."
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    citation={"source_id":"s","quotation":text,"location":{"character_start":0,"character_end":len(text)}}
    claim={"text":"$7.1 billion and -$2,000 million.","citations":[citation]}
    assert not flag_uncited_dollar_amounts([claim],[source])[0].get("uncited_dollar_amounts")
    changed={**claim,"text":"$7.1 million and $2 billion."}
    assert len(flag_uncited_dollar_amounts([changed],[source])[0]["uncited_dollar_amounts"])==2
    # A prefixed dollar symbol is not automatically treated as unqualified $.
    assert not flag_uncited_dollar_amounts([{**claim,"text":"C$4 billion","citations":[]}],[source])[0].get("uncited_dollar_amounts")


def test_explicit_quarter_labels_are_rechecked_against_located_quotes():
    from app.research_evidence import flag_uncited_quarters
    text = "The result starts in the fourth quarter of 2022. The horizon ends in 2025:Q1."
    source = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    def cite(quote):
        start=text.index(quote)
        return {"source_id":"s","quotation":quote,"location":{"character_start":start,"character_end":start+len(quote)}}
    claim={"text":"The 2022:Q4 actual value projects to 2025:Q1.", "citations":[cite("The result starts in the fourth quarter of 2022.")]}
    result=flag_uncited_quarters([claim],[source])[0]
    assert result["uncited_quarters"]==["2025:Q1"] and "uncited_quarters" not in claim
    added={**claim,"citations":claim["citations"]+[cite("The horizon ends in 2025:Q1.")]}
    assert not flag_uncited_quarters([added],[source])[0].get("uncited_quarters")
    assert flag_uncited_quarters([added],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0]["uncited_quarters"]
    stale={**added,"citations":[{**added["citations"][-1],"partial_token":True}]}
    assert flag_uncited_quarters([stale],[source])[0]["uncited_quarters"]==["2022:Q4","2025:Q1"]


def test_quarter_formats_are_distinct_and_ranges_do_not_produce_false_quarters():
    from app.research_evidence import _explicit_quarters
    assert [x[1] for x in _explicit_quarters("2022:Q4-2025:Q1")] == [(2022,4),(2025,1)]
    for text in ("2022:Q4", "Q4 2022", "fourth quarter of 2022", "2022年第四季度"):
        assert _explicit_quarters(text)[0][1] == (2022,4)
    assert _explicit_quarters("2025 and 4.5 percent") == []


def test_trade_deficit_direction_warning_requires_matching_located_components():
    from app.research_evidence import flag_component_direction
    text = "The deficit reflected a decrease in the goods deficit of $0.3 billion and a decrease in the services surplus of $1.6 billion."
    source = ResearchSource(source_id="s", owner_id="owner", title="Trade report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True, display=True))
    citation={"source_id":"s","quotation":text,"location":{"character_start":0,"character_end":len(text)}}
    claim={"text":"The goods deficit and services surplus fell, both of which would narrow the deficit.","citations":[citation]}
    flagged=flag_component_direction([claim],[source])[0]
    assert flagged["balance_direction_warnings"][0]["status"]=="requires_review"
    assert "balance_direction_warnings" not in claim
    for statement in ("The goods deficit narrowed and the smaller services surplus widened the combined deficit.",
                      "It is not true that both changes would narrow the deficit.",
                      "Both changes widened the deficit."):
        assert not flag_component_direction([{**claim,"text":statement}],[source])[0].get("balance_direction_warnings")
    assert not flag_component_direction([claim],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0].get("balance_direction_warnings")
    assert not flag_component_direction([{**claim,"citations":[{**citation,"partial_token":True}]}],[source])[0].get("balance_direction_warnings")
    assert not flag_component_direction([{**claim,"citations":[{**citation,"location":{"status":"ambiguous"}}]}],[source])[0].get("balance_direction_warnings")
    omitted={**claim,"citations":[{**citation,"quotation":"invented"}]}
    assert not flag_component_direction([omitted],[source])[0].get("balance_direction_warnings")


def test_revision_contradiction_warning_requires_original_located_revision():
    from app.research_evidence import flag_component_direction
    text="The estimate primarily reflects a downward revision to consumer spending."
    source=ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
        obtained_at=datetime.now(timezone.utc),content_sha256="test",permissions=SourcePermissions(store=True,display=True))
    cite={"source_id":"s","quotation":text,"location":{"character_start":0,"character_end":len(text)}}
    claim={"text":"The downward revision is contrary to a claim that spending rose.","citations":[cite]}
    flagged=flag_component_direction([claim],[source])[0]
    assert flagged["balance_direction_warnings"][0]["basis"]=="revision_vs_change"
    assert "balance_direction_warnings" not in claim
    assert not flag_component_direction([{**claim,"text":"The downward revision does not contradict growth."}],[source])[0].get("balance_direction_warnings")
    assert not flag_component_direction([{**claim,"text":"The downward revision reflects a lower earlier estimate."}],[source])[0].get("balance_direction_warnings")
    for bad in ({**cite,"partial_token":True},{**cite,"quotation":"invented"},{**cite,"location":{"status":"ambiguous"}}):
        assert not flag_component_direction([{**claim,"citations":[bad]}],[source])[0].get("balance_direction_warnings")
    assert not flag_component_direction([claim],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0].get("balance_direction_warnings")


def test_named_entity_prefix_context_requires_a_matching_claim_and_original_source():
    from app.research_evidence import flag_citation_subject_context
    text="Table A.2. Bank of America Corporation reported 11.9 percent."
    source=ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
        obtained_at=datetime.now(timezone.utc),content_sha256="test",permissions=SourcePermissions(store=True,display=True))
    quote="of America Corporation reported 11.9 percent."
    start=text.index(quote)
    citation={"source_id":"s","quotation":quote,"location":{"character_start":start,"character_end":start+len(quote)}}
    claim={"text":"Bank of America Corporation reported 11.9 percent.","citations":[citation]}
    flagged=flag_citation_subject_context([claim],[source])[0]
    warning=flagged["citation_context_warnings"][0]
    assert warning["context_metric"]=="named entity prefix"
    assert text[warning["context_character_start"]:warning["context_character_end"]]=="Bank "
    assert "citation_context_warnings" not in claim
    assert not flag_citation_subject_context([{**claim,"text":"America Corporation reported 11.9 percent."}],[source])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([claim],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([{**claim,"citations":[{**citation,"location":{"status":"ambiguous"}}]}],[source])[0].get("citation_context_warnings")
    full="Bank of America Corporation reported 11.9 percent.";full_start=text.index(full)
    corrected={"source_id":"s","quotation":full,"location":{"character_start":full_start,"character_end":full_start+len(full)}}
    assert not flag_citation_subject_context([{**claim,"citations":[corrected]}],[source])[0].get("citation_context_warnings")


def test_component_direction_flags_chinese_trade_claims_against_english_or_chinese_sources():
    from app.research_evidence import flag_component_direction
    quotes=["A decrease in the goods deficit and a decrease in the services surplus.",
            "商品逆差下降，服务顺差下降。"]
    for text in quotes:
        report = ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
            obtained_at=datetime.now(timezone.utc),content_sha256="test",permissions=SourcePermissions(store=True,display=True))
        citation={"source_id":"s","quotation":text,"location":{"character_start":0,"character_end":len(text)}}
        item={"text":"两项变化均会缩小总贸易逆差。","citations":[citation]}
        warning=flag_component_direction([item],[report])[0]["balance_direction_warnings"][0]
        assert warning["basis"]=="trade_deficit"
        assert not flag_component_direction([{**item,"text":"并非两项变化均会缩小总贸易逆差。"}],[report])[0].get("balance_direction_warnings")


def test_component_direction_handles_operating_profit_with_conservative_scope():
    from app.research_evidence import flag_component_direction
    for text,claim in (("Revenue fell and operating expenses fell.", "Both changes would increase operating profit."),
                       ("收入下降，营业费用下降。", "两项变化均会提升营业利润。"),
                       ("營收下降，營業費用下降。", "兩項變化均會提升營業利潤。")):
        report=ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
            obtained_at=datetime.now(timezone.utc),content_sha256="test",permissions=SourcePermissions(store=True,display=True))
        cite={"source_id":"s","quotation":text,"location":{"character_start":0,"character_end":len(text)}}
        result=flag_component_direction([{"text":claim,"citations":[cite]}],[report])[0]
        assert result["balance_direction_warnings"][0]["basis"]=="operating_profit"
        good="Revenue fell, which lowered operating profit, while lower expenses increased it."
        assert not flag_component_direction([{"text":good,"citations":[cite]}],[report])[0].get("balance_direction_warnings")
        assert not flag_component_direction([{"text":"It is not true that both changes would increase operating profit.","citations":[cite]}],[report])[0].get("balance_direction_warnings")


def test_component_direction_accepts_common_english_movement_word_orders():
    from app.research_evidence import flag_component_direction
    pairs=[("The goods deficit narrowed and the services surplus shrank.","Both changes would narrow the deficit.","trade_deficit"),
           ("A decline in revenue and a decrease in operating expenses.","Both changes would increase operating profit.","operating_profit")]
    for text,claim,basis in pairs:
        report=ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
            obtained_at=datetime.now(timezone.utc),content_sha256="test",permissions=SourcePermissions(store=True,display=True))
        citation={"source_id":"s","quotation":text,"location":{"character_start":0,"character_end":len(text)}}
        assert flag_component_direction([{"text":claim,"citations":[citation]}],[report])[0]["balance_direction_warnings"][0]["basis"]==basis


def test_directional_parenthesized_percentage_is_not_marked_uncited():
    claim = {"text": "PCE increased 0.1 percent.", "citations": [{"quotation": "PCE increased $23.7 billion (0.1 percent)."}]}
    assert not suggest_percentage_evidence([claim], [])[0].get("uncited_percentages")
    opposite = {**claim, "citations": [{"quotation": "PCE decreased $23.7 billion (0.1 percent)."}]}
    assert suggest_percentage_evidence([opposite], [])[0]["uncited_percentages"] == ["0.1 percent"]
    ambiguous = {**claim, "citations": [{"quotation": "The change was (0.1 percent)."}]}
    assert suggest_percentage_evidence([ambiguous], [])[0]["uncited_percentages"] == ["0.1 percent"]


def test_possessive_entity_before_a_quoted_metric_is_retained_as_context():
    text = "River is a subsidiary. River's revenue increased 5 percent."
    quote = "revenue increased 5 percent"
    start = text.index(quote)
    report = ResearchSource(source_id="s", owner_id="owner", title="Report", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="test", permissions=SourcePermissions(store=True,display=True))
    citation = {"source_id":"s", "quotation":quote, "location":{"character_start":start, "character_end":start+len(quote)}}
    item = {"text":"Harbor revenue increased 5 percent.", "citations":[citation]}
    warning = flag_citation_subject_context([item], [report])[0]["citation_context_warnings"][0]
    assert warning["context_metric"] == "possessive entity"
    assert text[warning["context_character_start"]:warning["context_character_end"]] == "River's "
    original = text.index("River's")
    full = {"source_id":"s", "quotation":text[original:start], "location":{"character_start":original,"character_end":start}}
    assert not flag_citation_subject_context([{**item, "citations":[citation, full]}], [report])[0].get("citation_context_warnings")
    assert not flag_citation_subject_context([item], [report.model_copy(update={"permissions":SourcePermissions(store=True,display=False)})])[0].get("citation_context_warnings")


def test_possessive_context_does_not_include_previous_sentence():
    text = "A report by Harbor Labs. River's revenue increased 5 percent."
    quote = "revenue increased 5 percent"; start = text.index(quote)
    report = ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
        obtained_at=datetime.now(timezone.utc),content_sha256="test",permissions=SourcePermissions(store=True,display=True))
    item = {"text":"Revenue rose 5 percent.","citations":[{"source_id":"s","quotation":quote,
            "location":{"character_start":start,"character_end":start+len(quote)}}]}
    warning = flag_citation_subject_context([item],[report])[0]["citation_context_warnings"][0]
    assert text[warning["context_character_start"]:warning["context_character_end"]] == "River's "


def test_repeated_percentage_requires_review_even_when_number_is_quoted():
    item = {"text": "Nominal spending grew 0.1 percent while the price index fell 0.1%.",
            "citations": [{"quotation": "The price index decreased 0.1 percent."}]}
    result = suggest_percentage_evidence([item], [])[0]
    assert result["reused_percentages"] == ["0.1 percent"]
    assert not result.get("uncited_percentages")
    assert "reused_percentages" not in item
    revised = {**item, "text": "Nominal spending grew 0.1 percent.", "reused_percentages": ["stale"]}
    assert not suggest_percentage_evidence([revised], [])[0].get("reused_percentages")
    formula = {"text": "(120-100)/100 × 100% and (90-80)/80 × 100%", "citations": []}
    assert not suggest_percentage_evidence([formula], [])[0].get("reused_percentages")


def test_percentage_upper_bound_is_not_the_same_as_an_exact_fact():
    from app.research_evidence import suggest_uncited_numeric_facts
    text = "PCE increased less than 0.1 percent. Services increased 0.1 percent."
    src = ResearchSource(source_id="s", owner_id="owner", title="Release", text=text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="hash", permissions=SourcePermissions(store=True, display=True))
    first = "PCE increased less than 0.1 percent."
    second = "Services increased 0.1 percent."
    start = text.index(second)
    fact = {"fact_id":"services", "source_id":"s", "value":"0.1", "unit":"percent", "quotation":second,
            "location":{"character_start":start,"character_end":start+len(second)},
            "verification":"model_extraction_not_independently_verified"}
    citation = {"source_id":"s", "quotation":first, "location":{"character_start":0,"character_end":len(first)}}
    claim = {"text":"PCE increased less than 0.1 percent.","citations":[citation]}
    assert not suggest_uncited_numeric_facts([claim],[fact],[src])[0].get("uncited_fact_values")
    checked = suggest_percentage_evidence([claim],[])[0]
    assert not checked.get("uncited_percentage_bounds")
    wrong = {**claim,"citations":[{"quotation":second}]}
    assert suggest_percentage_evidence([wrong],[])[0]["uncited_percentage_bounds"] == ["0.1 percent"]
    mixed = {**claim,"text":"PCE increased less than 0.1 percent and services increased 0.1 percent."}
    assert suggest_uncited_numeric_facts([mixed],[fact],[src])[0]["uncited_fact_values"] == ["0.1 percent"]
    assert not suggest_percentage_evidence([{**claim,"text":"PCE rose below 0.1%."}],[])[0].get("uncited_percentage_bounds")
    for negated in ("PCE increased not less than 0.1 percent.", "PCE increased no less than 0.1 percent.", "PCE 增长不少于 0.1%。"):
        assert not suggest_percentage_evidence([{**claim,"text":negated,"citations":[{"quotation":second}]}],[])[0].get("uncited_percentage_bounds")
        assert suggest_uncited_numeric_facts([{**claim,"text":negated}],[fact],[src])[0].get("uncited_fact_values")


def test_uncited_negative_change_can_be_suggested_without_proving_semantic_support():
    from app.research_evidence import suggest_uncited_numeric_facts
    text="Services increased $33.3 billion. Goods had a decrease of $9.6 billion in spending."
    source=ResearchSource(source_id="s",owner_id="owner",title="Release",text=text,kind="user_supplied",
        obtained_at=datetime.now(timezone.utc),content_sha256="hash",permissions=SourcePermissions(store=True,display=True))
    quoted="a decrease of $9.6 billion in spending"; start=text.index(quoted)
    candidate={"fact_id":"goods","source_id":"s","value":"-9.6","unit":"billion USD","currency":"USD",
        "period":"2022-07","quotation":quoted,"location":{"character_start":start,"character_end":start+len(quoted)},
        "verification":"model_extraction_not_independently_verified","direction_context_unverified":True}
    other="Services increased $33.3 billion"; start_other=text.index(other)
    claim={"text":"Goods spending fell in nominal terms ($9.6 billion).","role":"counterargument",
        "citations":[{"source_id":"s","quotation":other,"location":{"character_start":start_other,"character_end":start_other+len(other)}}]}
    found=suggest_uncited_numeric_facts([claim],[candidate],[source])[0]
    assert found["uncited_fact_values"]==["-9.6 billion USD"]
    assert found["suggested_fact_evidence"][0]["fact_id"]=="goods"
    assert found["suggested_fact_evidence"][0]["status"]=="requires_semantic_review"
    assert len(found["citations"])==1 and found["citations"][0]["quotation"]==other
    for changed in ("Goods did not fall in nominal terms ($9.6 billion).", "Goods fell in nominal terms ($9.7 billion)."):
        assert not suggest_uncited_numeric_facts([{**claim,"text":changed}],[candidate],[source])[0].get("suggested_fact_evidence")
    assert not suggest_uncited_numeric_facts([claim],[{**candidate,"direction_context_unverified":False}],[source])[0].get("suggested_fact_evidence")
    assert not suggest_uncited_numeric_facts([claim],[candidate,{**candidate,"fact_id":"other"}],[source])[0].get("suggested_fact_evidence")
    assert not suggest_uncited_numeric_facts([claim],[candidate],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[0].get("suggested_fact_evidence")


def test_related_claim_quotation_is_limited_to_unique_visible_matching_source():
    from app.research_evidence import flag_uncited_dollar_amounts, suggest_related_claim_quotations
    text="Services increased $33.3 billion while goods decreased $9.6 billion."
    src=ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
        obtained_at=datetime.now(timezone.utc),content_sha256="hash",permissions=SourcePermissions(store=True,display=True))
    full=text;short="Services increased $33.3 billion"
    claims=[{"text":"Services rose and goods fell.","citations":[{"source_id":"s","quotation":full,"location":{"character_start":0,"character_end":len(full)}}]},
            {"text":"Goods fell $9.6 billion.","citations":[{"source_id":"s","quotation":short,"location":{"character_start":0,"character_end":len(short)}}]}]
    flagged=flag_uncited_dollar_amounts(claims,[src]);result=suggest_related_claim_quotations(flagged,[src]);candidate=result[1]["suggested_related_quotations"][0]
    assert candidate["quotation"]==full and candidate["matched_amounts"]==["$9.6 billion"]
    assert candidate["from_claim_index"]==0 and candidate["status"]=="requires_semantic_review"
    assert len(result[1]["citations"])==1 and "suggested_related_quotations" not in claims[1]
    edited=[dict(c) for c in claims];edited[1]={**edited[1],"citations":[*edited[1]["citations"],claims[0]["citations"][0]]}
    assert not suggest_related_claim_quotations(flag_uncited_dollar_amounts(edited,[src]),[src])[1].get("suggested_related_quotations")
    hidden=src.model_copy(update={"permissions":SourcePermissions(store=True)})
    assert not suggest_related_claim_quotations(flag_uncited_dollar_amounts(claims,[hidden]),[hidden])[1].get("suggested_related_quotations")
    duplicated=src.model_copy(update={"text":text+" "+text})
    assert not suggest_related_claim_quotations(flag_uncited_dollar_amounts(claims,[duplicated]),[duplicated])[1].get("suggested_related_quotations")
    changed=[claims[0],{**claims[1],"text":"Goods fell $9.6 million."}]
    assert not suggest_related_claim_quotations(flag_uncited_dollar_amounts(changed,[src]),[src])[1].get("suggested_related_quotations")
    split_donor=[{**claims[0],"citations":[{**claims[0]["citations"][0],"quotation_match":"split_ellipsis"}]},claims[1]]
    assert not suggest_related_claim_quotations(flag_uncited_dollar_amounts(split_donor,[src]),[src])[1].get("suggested_related_quotations")


def test_related_quotation_suggests_percentages_from_table_for_ambiguous_target():
    from app.research_evidence import suggest_related_claim_quotations
    note="The projections are hypothetical."
    table="Bank capital ratios, percent: actual 14.6, projected 15.0, minimum 12.6."
    text=note+" "+table+" "+note
    source=ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
        obtained_at=datetime.now(timezone.utc),content_sha256="test",permissions=SourcePermissions(store=True,display=True))
    start=text.index(table);last=text.rindex(note)
    donor={"text":"Bank ratios","citations":[{"source_id":"s","quotation":table,
        "location":{"character_start":start,"character_end":start+len(table)}}]}
    ambiguous={"source_id":"s","quotation":note,"location":{"status":"ambiguous","matches":[
        {"character_start":0,"character_end":len(note)},{"character_start":last,"character_end":last+len(note)}]}}
    target={"text":"Actual 14.6%, projected 15.0%, minimum 12.6%.","uncited_percentages":["14.6%","15.0%","12.6%"],"citations":[ambiguous]}
    out=suggest_related_claim_quotations([donor,target],[source])
    candidate=out[1]["suggested_related_quotations"][0]
    assert candidate["quotation"]==table and set(candidate["matched_amounts"])=={"14.6%","15.0%","12.6%"}
    assert candidate["status"]=="requires_semantic_review" and len(out[1]["citations"])==1
    assert "suggested_related_quotations" not in target
    bad={**ambiguous,"location":{"status":"ambiguous","matches":[{"character_start":1,"character_end":len(note)}]}}
    assert not suggest_related_claim_quotations([donor,{**target,"citations":[bad]}],[source])[1].get("suggested_related_quotations")
    assert not suggest_related_claim_quotations([donor,target],[source.model_copy(update={"permissions":SourcePermissions(store=True)})])[1].get("suggested_related_quotations")
    duplicated=source.model_copy(update={"text":text+" "+table})
    assert not suggest_related_claim_quotations([donor,target],[duplicated])[1].get("suggested_related_quotations")


def test_exact_dates_require_located_authorized_quotations_not_source_metadata():
    from datetime import datetime, timezone
    from app.research_evidence import flag_uncited_exact_dates
    text = 'Published January 25, 2024. GDP increased 2.5 percent.'
    report = ResearchSource(source_id='s', owner_id='owner', title='Release', text=text, kind='user_supplied',
        published_at=datetime(2024, 1, 25, tzinfo=timezone.utc), obtained_at=datetime.now(timezone.utc),
        content_sha256='hash', permissions=SourcePermissions(store=True, display=True))
    def citation(quote):
        start = text.index(quote)
        return {'source_id': 's', 'quotation': quote, 'location': {'character_start': start, 'character_end': start + len(quote)}}
    statement = {'text': "BEA's January 25, 2024 advance estimate reported growth.", 'citations': [citation('GDP increased 2.5 percent.')]}
    assert flag_uncited_exact_dates([statement], [report])[0]['uncited_exact_dates'] == ['January 25, 2024']
    supported = {**statement, 'text': '2024年1月25日发布。', 'citations': [citation('Published January 25, 2024.')]}
    assert not flag_uncited_exact_dates([supported], [report])[0].get('uncited_exact_dates')
    bad_location = {**supported, 'citations': [{**supported['citations'][0], 'location': {'status': 'ambiguous'}}]}
    assert flag_uncited_exact_dates([bad_location], [report])[0]['uncited_exact_dates'] == ['2024年1月25日']
    simulated = report.model_copy(update={'is_simulated': True})
    assert flag_uncited_exact_dates([supported], [simulated])[0]['uncited_exact_dates']
    assert not flag_uncited_exact_dates([{'text': 'The period was 2023 Q4.', 'uncited_exact_dates': ['stale']}], [report])[0].get('uncited_exact_dates')
