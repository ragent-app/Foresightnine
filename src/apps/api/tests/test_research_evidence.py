from datetime import datetime, timezone
from app.research_evidence import claim_amount_differences, evidence_differences, flag_publication_date_mismatches, flag_rate_level_point_warnings, flag_citation_subject_context, suggest_percentage_evidence, suggest_usd_amount_evidence
from app.research_sources import ResearchSource, SourcePermissions


def fact(fact_id, source_id, value, *, label="Revenue", period="FY2023", unit="USD million", currency="USD"):
    return {"fact_id": fact_id, "source_id": source_id, "value": value,
            "label": label, "period": period, "unit": unit, "currency": currency}


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
