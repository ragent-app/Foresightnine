"""Keep user-specific details out of public discovery, before quota and network I/O."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from collections.abc import Iterable

# Public catalog and document lookups need a subject or terms, never contact or
# account credentials. Keep the original user-authored text in the account.
_CONTACT = re.compile(
    r'[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:sk-|sk_)[a-z0-9_-]{12,}\b|'
    r'\b(?:iban\s*[:=]?\s*)?[a-z]{2}\d{2}(?:[ -]?[a-z0-9]){11,30}\b|'
    r'(?<!\w)(?:\+\d{1,3}[ -]?)?\(?\d{3}\)?[ -]\d{3}[ -]\d{4}(?!\w)|'
    r'(?<!\d)(?:1[3-9]\d{9}|\d{15,19}[xX]?)(?!\d)|'
    r'(?:账号|帐号|身份证号?|账户号码|卡号|account\s+(?:number|no\.)|passport\s+(?:number|no\.))'
    r'\s*[:：#=]?\s*[a-z0-9*][a-z0-9*-]{3,}', re.I)
_FIRST_PERSON = re.compile(r'我|本人|我们|俺|家里|\b(?:me|my|mine|our|ours|we|i(?=\s+(?:am|have|need|want|own|hold|can|will|earn|plan|invest|pay|owe)\b))\b', re.I)
_PRIVATE_AMOUNT = re.compile(
    r'(?:[$€£¥￥]\s*\d[\d,.]*(?:\s*[kmb]\b)?|'
    r'\d[\d,.]*(?:\.\d+)?\s*(?:万|亿|元|圆|美元|欧元|英镑|人民币|usd\b|eur\b|dollars?\b|euros?\b|k\b)|'
    r'[零〇一二两三四五六七八九十百千]+[万亿](?:元|块|美元|人民币)?)', re.I)
_NUMBER = re.compile(r'\d[\d,]*(?:\.\d+)?')
_PRIVATE_BALANCE = re.compile(
    r'(?:薪资|工资|收入|余额|存款|学费|负债|salary|savings|balance|income|tuition|debt)\s*'
    r'(?:约为|为|是|约|is|of|about)?\s*[:：]?\s*(?:[$€£¥￥]\s*)?\d[\d,]*(?:\.\d+)?(?:\s*[kKmM]\b|万|亿)?', re.I)
_AMOUNT_TOKEN = re.compile(r'(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(万|亿|[kmb]\b)?', re.I)


def _amount_values(value: str) -> set[Decimal]:
    units = {'k': 1000, 'm': 1000000, 'b': 1000000000, '万': 10000, '亿': 100000000}
    result = set()
    for number, suffix in _AMOUNT_TOKEN.findall(value):
        try:
            result.add(Decimal(number.replace(',', '')) * units.get(suffix.lower(), 1))
        except InvalidOperation:
            continue
    return result


_NAME = re.compile(r'(?:我叫|我的名字是|姓名[：:]\s*)([\u4e00-\u9fff]{2,4})|'
                   r'(?i:\bmy name is)\s+([A-Z][a-z]+(?: [A-Z][a-z]+){0,3})')


def minimize_identity(text: str) -> str:
    """Redact incidental identifiers for model-bound user messages, not source quotations."""
    return _CONTACT.sub('[private identifier]', _NAME.sub('[private name]', text))


def public_query_problem(query: str, user_texts: Iterable[str]) -> str | None:
    """Reject personal lookups; a clean public reformulation is independently retryable."""
    text = unicodedata.normalize('NFKC', query)
    if _CONTACT.search(text) or _FIRST_PERSON.search(text) or _NAME.search(text):
        return 'private_query'
    compact = re.sub(r'[\s,]', '', text.casefold())
    query_values = _amount_values(text)
    for original in user_texts:
        raw = unicodedata.normalize('NFKC', original)
        for match in _NAME.finditer(raw):
            name = next((s for s in match.groups() if s), '')
            if name and name.casefold() in text.casefold():
                return 'private_query'
        if not _FIRST_PERSON.search(raw):
            continue
        for amount in [*_PRIVATE_AMOUNT.findall(raw), *_PRIVATE_BALANCE.findall(raw)]:
            normalized = re.sub(r'[\s,]', '', amount.casefold())
            if normalized in compact or query_values.intersection(_amount_values(amount)):
                return 'private_query'
            numbers = _NUMBER.findall(normalized)
            if any(number in _NUMBER.findall(compact) for number in numbers):
                return 'private_query'
    return None


def private_query_result() -> dict:
    return {'status': 'needs_attention', 'reason': 'private_query',
            'message': 'Search only for public product names, rules or source terms. Keep personal identity, '
                       'balances, account details and individual circumstances within this research turn. '
                       'Choose a public reformulation or use the authorized local materials.'}
