"""Conservative textual matching for future financial research evaluations.

These functions identify limited equivalent representations. They do not
establish source support, entities, periods, or complete semantic correctness.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

_NUMBER = r"[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_AMOUNT_ZH = re.compile(rf"(?<![\d,.])(?P<value>{_NUMBER})\s*(?P<unit>万亿|千亿|百亿|十亿|亿|千万|百万|十万|万|千)?\s*(?:美元|美金)")
_MILLION_FACTORS = {"万亿": Decimal("1000000"), "千亿": Decimal("100000"), "百亿": Decimal("10000"),
    "十亿": Decimal("1000"), "亿": Decimal("100"), "千万": Decimal("10"), "百万": Decimal("1"),
    "十万": Decimal("0.1"), "万": Decimal("0.01"), "千": Decimal("0.001"), "": Decimal("0.000001")}
_DECREASE_ZH = re.compile(r"(?:下降|下跌|下滑|减少|回落|降低|萎缩)(?:了|约|了约)?\s*$")
_DECREASE_EN = re.compile(r"\b(?:decreased|declined|fell|dropped|contracted)(?:\s+by)?\s*$", re.IGNORECASE)
_NEGATION = re.compile(r"(?:没有|并非|不是|未必|未曾|未|不|没)|\b(?:not|never|neither|no)\b|didn['’]t", re.IGNORECASE)
_NO_VOTE_ZH = re.compile(r"(?:没有|无人|无|零)[\u4e00-\u9fff\s]{0,12}(?:反对票|反对|异议)")
_NO_VOTE_EN = re.compile(r"\b(?:no\s+dissent(?:s)?|no\s+votes?\s+against|none\s+voted\s+against|no\s+one\s+voted\s+against|unanimous(?:ly)?)\b", re.IGNORECASE)


def _decimal(value: str) -> Decimal | None:
    if len(value) > 40:
        return None
    try:
        result = Decimal(value.replace(",", "").replace("−", "-"))
        return result if result.is_finite() and abs(result.adjusted()) <= 30 else None
    except InvalidOperation:
        return None


def mentions_usd_millions(text: str, expected: str) -> bool:
    """Match explicit Chinese USD-unit conversions exactly, without rounding."""
    target = _decimal(expected)
    if target is None or len(text) > 3000:
        return False
    for match in _AMOUNT_ZH.finditer(text):
        number = _decimal(match.group("value"))
        if number is not None:
            with localcontext() as context:
                context.prec = 100
                if number * _MILLION_FACTORS[match.group("unit") or ""] == target:
                    return True
    return False


def allowed_percentage(text: str, start: int, number: str, allowed: list[str]) -> bool:
    """Match a literal percentage or a positive magnitude explicitly described as a decrease."""
    value = _decimal(number)
    places = len(number.rsplit('.', 1)[1]) if '.' in number else 0
    if value is None or places > 8 or not 0 <= start <= len(text) or len(text) > 3000:
        return False
    prefix = text[max(0, start - 40):start]
    direction = _DECREASE_ZH.search(prefix) or _DECREASE_EN.search(prefix)
    if direction and _NEGATION.search(prefix[max(0, direction.start() - 16):direction.start()]):
        return False
    # "decreased -0.6%" has ambiguous or contradictory direction.
    if direction and value < 0:
        return False
    comparison = -value if direction and value > 0 else value
    for raw in allowed:
        target = _decimal(raw)
        if target is None:
            continue
        try:
            if target.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) == comparison:
                return True
        except InvalidOperation:
            continue
    return False


def mentions_no_dissent(text: str) -> bool:
    """Recognize limited affirmative no-dissent phrases; source still needs checking."""
    if len(text) > 3000:
        return False
    for pattern in (_NO_VOTE_ZH, _NO_VOTE_EN):
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 12):match.start()]
            body = re.sub(r"^(?:没有|无人|无|零)", "", match.group()) if pattern is _NO_VOTE_ZH else ""
            if not _NEGATION.search(before) and not _NEGATION.search(body):
                return True
    return False
