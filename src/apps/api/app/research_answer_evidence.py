"""Resolve answer dependencies in the exact research version being saved or read."""
from __future__ import annotations

from collections.abc import Callable, Iterator

from .research_topics import QuestionPartReference, ResearchContent, ResearchGap, ResearchQuestionPart, ResearchTurn


def part_references(part: ResearchQuestionPart) -> Iterator[QuestionPartReference]:
    yield from part.references
    for requirement in part.requirements:
        yield from requirement.references


class AnswerEvidence:
    def __init__(self, content: ResearchContent):
        self.facts = {fact.fact_id: fact for fact in content.facts}
        self.calculations = {calc.calculation_id: calc for calc in content.calculations}
        self.claims = {claim.claim_id: claim for claim in content.claims}
        self.charts = {chart.chart_id: chart for chart in content.charts}
        self.analyses = {a.analysis_id: a for t in content.turns for a in t.market_analyses}
        self.sources = {source["source_id"] for source in content.sources if source.get("source_id")}

    def source_ids(self, ref: QuestionPartReference) -> set[str]:
        """Raise KeyError for a missing dependency, including a calculation input."""
        sources = set()
        facts = {ref.fact_id} if ref.fact_id else set()
        calculations = {ref.calculation_id} if ref.calculation_id else set()
        if ref.analysis_id:
            analysis = self.analyses[ref.analysis_id]
            sources.update(analysis.result["source_ids"])
        if ref.chart_id:
            chart = self.charts[ref.chart_id]
            sources.update(chart.source_ids)
            sources.update(point.provenance.source_id for series in chart.series for point in series.points)
        if ref.source_id:
            if ref.source_id not in self.sources:
                raise KeyError(ref.source_id)
            sources.add(ref.source_id)
        if ref.claim_id:
            claim = self.claims[ref.claim_id]
            sources.update(citation.source_id for citation in claim.citations)
            calculations.update(claim.calculation_ids)
            facts.update(claim.fact_ids)
        for cid in calculations:
            facts.update(row.fact_id for row in self.calculations[cid].inputs)
        sources.update(self.facts[fid].source_id for fid in facts)
        if not sources:
            raise KeyError("No evidence source")
        return sources

    def intact(self, ref: QuestionPartReference) -> bool:
        try:
            self.source_ids(ref)
            return True
        except KeyError:
            return False


def reconcile_saved_answers(content: ResearchContent, turn: ResearchTurn) -> None:
    """Run after item-level fault isolation, before committing status or report."""
    if not turn.question_parts:
        return
    evidence = AnswerEvidence(content)
    reason = "部分依据未能保存，请根据原始资料重新核验。"
    invalid_core = False
    for part in turn.question_parts:
        if all(evidence.intact(ref) for ref in part_references(part)):
            continue
        part.references = [ref for ref in part.references if evidence.intact(ref)]
        for requirement in part.requirements:
            if any(not evidence.intact(ref) for ref in requirement.references):
                requirement.references = [ref for ref in requirement.references if evidence.intact(ref)]
                requirement.status = "unavailable"
                requirement.reason = reason
        part.answer, part.reason, part.status = "", reason, "unresolved"
        invalid_core |= part.core
    if invalid_core:
        turn.status = "partial"
        turn.report = ""
        turn.partial_notes = None
        if not any(gap.code == "answer_evidence_unavailable" for gap in turn.gaps):
            turn.gaps.append(ResearchGap(code="answer_evidence_unavailable", message=reason))


def visible_answer_content(content: ResearchContent, can_display: Callable[[str], bool]) -> ResearchContent:
    """Restrict a read projection after source access changes; keep the stored version immutable."""
    evidence = AnswerEvidence(content)
    used = set(evidence.sources)
    used.update(sid for a in evidence.analyses.values() for sid in a.result["source_ids"])
    used.update(f.source_id for f in content.facts)
    for chart in content.charts:
        used.update(chart.source_ids)
        used.update(point.provenance.source_id for series in chart.series for point in series.points)
    def table_sources(table):
        return set(table.source_ids) | {cell.provenance.source_id for row in table.rows
                                        for cell in row.cells.values() if cell.provenance}
    for table in content.tables:
        used.update(table_sources(table))
    used.update(c.source_id for claim in content.claims for c in claim.citations)
    used.update(r.source_id for turn in content.turns for r in turn.read_ranges)
    used.update(ref.source_id for turn in content.turns for part in turn.question_parts
                for ref in part_references(part) if ref.source_id)
    hidden = {sid for sid in used if not can_display(sid)}
    if not hidden:
        return content

    safe = content.model_copy(deep=True)
    def allowed(ref: QuestionPartReference) -> bool:
        try:
            return not hidden.intersection(evidence.source_ids(ref))
        except KeyError:
            return False

    safe.sources = [row for row in safe.sources if row.get("source_id") not in hidden]
    safe.facts = [f for f in safe.facts if f.source_id not in hidden]
    safe.calculations = [c for c in safe.calculations if allowed(QuestionPartReference(calculation_id=c.calculation_id))]
    safe.claims = [c for c in safe.claims if allowed(QuestionPartReference(claim_id=c.claim_id))]
    safe.charts = [c for c in safe.charts if allowed(QuestionPartReference(chart_id=c.chart_id))]
    safe.tables = [t for t in safe.tables if t.source_ids and not hidden.intersection(table_sources(t))]
    safe.source_candidates = []
    for turn in safe.turns:
        # Free text, tool events and aggregate views can contain observations from multiple sources.
        # Retain only answers that have their own complete, currently displayable evidence chain.
        turn.report, turn.partial_notes, turn.stopping_note = "", None, None
        for instruction in turn.user_instructions:
            instruction.reason = None
            instruction.references = [ref for ref in instruction.references if allowed(ref)]
            # A report excerpt can reproduce an observation whose source was withdrawn.
            instruction.text_locations = [loc for loc in instruction.text_locations if loc.instruction_sequence]
            if not (instruction.references or instruction.text_locations):
                instruction.delivery_status = "unresolved"
        turn.events, turn.coverage_map, turn.reconciliation = [], None, None
        turn.market_analyses = [a for a in turn.market_analyses if not hidden.intersection(a.result["source_ids"])]
        turn.counter_evidence = None
        turn.read_ranges = [r for r in turn.read_ranges if r.source_id not in hidden]
        turn.status, turn.reason = "partial", "source_permission_changed"
        turn.gaps = [ResearchGap(code="source_permission_changed", message="部分依据的授权、研究时点或来源版本已变更。")]
        for part in turn.question_parts:
            refs = list(part_references(part))
            if not refs or not all(allowed(ref) for ref in refs):
                part.answer, part.reason, part.status = "", "此项依据当前不可用。", "unresolved"
                part.references, part.requirements = [], []
    safe.gaps = (list(safe.turns[-1].gaps) if safe.turns else
                 [ResearchGap(code="source_permission_changed", message="部分依据的授权、研究时点或来源版本已变更。")])
    return safe
