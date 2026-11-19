"""Incremental zero-eval streaming JSON extractor for submit_research draft reports."""
from __future__ import annotations


_WHITESPACE = frozenset(' \t\r\n')
_PRIMITIVE_START = frozenset('-0123456789tfn')
_PRIMITIVE_CONT = frozenset('-+.0123456789Eeabflnrstu')
_SIMPLE_ESCAPES = {
    '"': '"',
    '\\': '\\',
    '/': '/',
    'b': '\b',
    'f': '\f',
    'n': '\n',
    'r': '\r',
    't': '\t',
}
_HEX_DIGITS = frozenset('0123456789abcdefABCDEF')


class TopLevelReportExtractor:
    """Incrementally decode ONLY the top-level ``"report"`` string in a JSON object.

    Tracks container nesting and JSON string/escape boundaries across arbitrary
    chunk splits, handles ``\\uXXXX`` and UTF-16 surrogate pairs without ever
    emitting unpaired surrogates, ignores nested ``"report"`` keys, and stops
    emitting silently if the input stops being valid JSON.
    """

    def __init__(self) -> None:
        self.state: str = 'INIT'
        self.stack: list[str] = []
        self.failed: bool = False
        self.finished: bool = False
        self._seen_report: bool = False
        self._current_key_parts: list[str] = []
        self._current_key_len: int = 0
        self._current_key: str | None = None

        # Active string-decoding sub-state shared across chunks
        self._in_escape: bool = False
        self._unicode_hex: list[str] | None = None
        self._pending_high_surrogate: int | None = None

        # Sub-state while skipping nested arrays/objects
        self._skip_in_string: bool = False
        self._skip_escaped: bool = False

    def _fail(self) -> None:
        self.failed = True
        self.state = 'FAILED'
        self._pending_high_surrogate = None
        self._unicode_hex = None
        self._in_escape = False

    def _reset_string_decoder(self) -> None:
        self._in_escape = False
        self._unicode_hex = None
        self._pending_high_surrogate = None

    def _step_string_char(self, ch: str, emit: list[str] | None, *, collect_key: bool = False) -> str:
        """Advance string decoder by one char.

        Returns ``'done'`` when unescaped closing quote is seen, ``'more'`` otherwise,
        or ``'fail'`` on malformed escape/surrogate/control char.
        """
        if self._unicode_hex is not None:
            if ch not in _HEX_DIGITS:
                return 'fail'
            self._unicode_hex.append(ch)
            if len(self._unicode_hex) < 4:
                return 'more'
            cp = int(''.join(self._unicode_hex), 16)
            self._unicode_hex = None
            if self._pending_high_surrogate is not None:
                if 0xDC00 <= cp <= 0xDFFF:
                    combined = 0x10000 + ((self._pending_high_surrogate - 0xD800) << 10) + (cp - 0xDC00)
                    self._pending_high_surrogate = None
                    decoded = chr(combined)
                else:
                    return 'fail'
            elif 0xD800 <= cp <= 0xDBFF:
                self._pending_high_surrogate = cp
                return 'more'
            elif 0xDC00 <= cp <= 0xDFFF:
                return 'fail'
            else:
                decoded = chr(cp)
            if emit is not None:
                emit.append(decoded)
            elif collect_key and self._current_key_len < 256:
                self._current_key_parts.append(decoded)
                self._current_key_len += 1
            return 'more'

        if self._in_escape:
            self._in_escape = False
            if ch == 'u':
                self._unicode_hex = []
                return 'more'
            if self._pending_high_surrogate is not None:
                return 'fail'
            mapped = _SIMPLE_ESCAPES.get(ch)
            if mapped is None:
                return 'fail'
            if emit is not None:
                emit.append(mapped)
            elif collect_key and self._current_key_len < 256:
                self._current_key_parts.append(mapped)
                self._current_key_len += 1
            return 'more'

        if ch == '\\':
            self._in_escape = True
            return 'more'

        if self._pending_high_surrogate is not None:
            # High surrogate must be followed immediately by a \uDC00-\uDFFF escape.
            return 'fail'

        if ch == '"':
            return 'done'

        code = ord(ch)
        if code < 0x20 or 0xD800 <= code <= 0xDFFF:
            return 'fail'

        if emit is not None:
            emit.append(ch)
        elif collect_key and self._current_key_len < 256:
            self._current_key_parts.append(ch)
            self._current_key_len += 1
        return 'more'

    def feed(self, chunk: str) -> str:
        """Consume a JSON fragment chunk and return newly decoded top-level report text."""
        if not chunk or self.failed or self.finished:
            return ''
        emitted: list[str] = []
        for ch in chunk:
            if self.failed or self.finished:
                break
            st = self.state
            if st == 'INIT':
                if ch in _WHITESPACE:
                    continue
                if ch == '{':
                    self.stack.append('{')
                    self.state = 'TOP_KEY_OR_END'
                else:
                    self._fail()
            elif st in ('TOP_KEY_OR_END', 'TOP_KEY_REQUIRED'):
                if ch in _WHITESPACE:
                    continue
                if ch == '}' and st == 'TOP_KEY_OR_END':
                    self.stack.pop()
                    self.state = 'DONE'
                    self.finished = True
                elif ch == '"':
                    self._current_key_parts = []
                    self._current_key_len = 0
                    self._current_key = None
                    self._reset_string_decoder()
                    self.state = 'TOP_KEY'
                else:
                    self._fail()
            elif st == 'TOP_KEY':
                status = self._step_string_char(ch, None, collect_key=True)
                if status == 'fail':
                    self._fail()
                elif status == 'done':
                    self._current_key = ''.join(self._current_key_parts)
                    self._current_key_parts = []
                    self.state = 'TOP_COLON'
            elif st == 'TOP_COLON':
                if ch in _WHITESPACE:
                    continue
                if ch == ':':
                    self.state = 'TOP_VALUE'
                else:
                    self._fail()
            elif st == 'TOP_VALUE':
                if ch in _WHITESPACE:
                    continue
                if ch == '"':
                    self._reset_string_decoder()
                    if self._current_key == 'report' and not self._seen_report:
                        self._seen_report = True
                        self.state = 'REPORT_STR'
                    else:
                        self.state = 'SKIP_STR'
                elif ch == '{':
                    self.stack.append('{')
                    self._skip_in_string = False
                    self._skip_escaped = False
                    self.state = 'SKIP_STRUCT'
                elif ch == '[':
                    self.stack.append('[')
                    self._skip_in_string = False
                    self._skip_escaped = False
                    self.state = 'SKIP_STRUCT'
                elif ch in _PRIMITIVE_START:
                    self.state = 'SKIP_PRIMITIVE'
                else:
                    self._fail()
            elif st == 'REPORT_STR':
                status = self._step_string_char(ch, emitted, collect_key=False)
                if status == 'fail':
                    self._fail()
                elif status == 'done':
                    self.state = 'TOP_COMMA_OR_END'
            elif st == 'SKIP_STR':
                status = self._step_string_char(ch, None, collect_key=False)
                if status == 'fail':
                    self._fail()
                elif status == 'done':
                    self.state = 'TOP_COMMA_OR_END'
            elif st == 'SKIP_PRIMITIVE':
                if ch in _PRIMITIVE_CONT:
                    continue
                if ch in _WHITESPACE:
                    self.state = 'TOP_COMMA_OR_END'
                elif ch == ',':
                    self.state = 'TOP_KEY_REQUIRED'
                elif ch == '}':
                    self.stack.pop()
                    self.state = 'DONE'
                    self.finished = True
                else:
                    self._fail()
            elif st == 'SKIP_STRUCT':
                if self._skip_in_string:
                    if self._skip_escaped:
                        self._skip_escaped = False
                        continue
                    if ch == '\\':
                        self._skip_escaped = True
                    elif ch == '"':
                        self._skip_in_string = False
                    elif ord(ch) < 0x20:
                        self._fail()
                    continue
                if ch == '"':
                    self._skip_in_string = True
                    self._skip_escaped = False
                elif ch == '{':
                    self.stack.append('{')
                elif ch == '[':
                    self.stack.append('[')
                elif ch == '}':
                    if not self.stack or self.stack[-1] != '{':
                        self._fail()
                    else:
                        self.stack.pop()
                        if len(self.stack) == 1:
                            self.state = 'TOP_COMMA_OR_END'
                elif ch == ']':
                    if not self.stack or self.stack[-1] != '[':
                        self._fail()
                    else:
                        self.stack.pop()
                        if len(self.stack) == 1:
                            self.state = 'TOP_COMMA_OR_END'
            elif st == 'TOP_COMMA_OR_END':
                if ch in _WHITESPACE:
                    continue
                if ch == ',':
                    self.state = 'TOP_KEY_REQUIRED'
                elif ch == '}':
                    self.stack.pop()
                    self.state = 'DONE'
                    self.finished = True
                else:
                    self._fail()
            else:
                self._fail()
        return ''.join(emitted)
