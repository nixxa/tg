"""Turn message text into wrapped, styled lines.

Telegram stores formatting as entities. Messages that were typed as
Markdown and never parsed still contain the markers, and those are
rendered the same way.
"""

import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from tg.utils import split_string_dwc, string_len_dwc

BOLD = 1
ITALIC = 2
UNDERLINE = 4
STRIKE = 8
CODE = 16
PRE = 32
LINK = 64
QUOTE = 128
SPOILER = 256

_STYLE_BITS = (
    ("bold", BOLD),
    ("italic", ITALIC),
    ("underline", UNDERLINE),
    ("strike", STRIKE),
    ("code", CODE),
    ("pre", PRE),
    ("link", LINK),
    ("quote", QUOTE),
    ("spoiler", SPOILER),
)

_ENTITY_BITS = {
    "textEntityTypeBold": BOLD,
    "textEntityTypeItalic": ITALIC,
    "textEntityTypeUnderline": UNDERLINE,
    "textEntityTypeStrikethrough": STRIKE,
    "textEntityTypeCode": CODE,
    "textEntityTypePre": PRE,
    "textEntityTypeTextUrl": LINK,
    "textEntityTypeUrl": LINK,
    "textEntityTypeBlockQuote": QUOTE,
    "textEntityTypeSpoiler": SPOILER,
}

_FORMATTING_BITS = (
    BOLD | ITALIC | UNDERLINE | STRIKE | CODE | PRE | QUOTE | SPOILER
)

Run = Tuple[str, int]
Line = List[Tuple[str, FrozenSet[str]]]


def layout_message(
    text: str,
    entities: Optional[List[Dict[str, Any]]],
    width: int,
    markdown: bool = True,
) -> List[Line]:
    """Wrap text into display lines of styled pieces."""
    width = max(width, 1)
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return []
    runs = _runs(text, entities or [], markdown)
    lines = _wrap(runs, width)
    return _collapse_blank_lines(lines)


def preview_plain(text: str) -> str:
    """One-line preview with Markdown markers removed."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if looks_like_markdown(text):
        text = "".join(part for part, _style in parse_markdown(text))
    return " ".join(text.split())


def looks_like_markdown(text: str) -> bool:
    if "```" in text or "**" in text or "~~" in text:
        return True
    if re.search(r"(^|\n)#{1,6} \S", text):
        return True
    if re.search(r"(^|\n)> \S", text):
        return True
    if re.search(r"\[[^\]]+\]\([^)]+\)", text):
        return True
    if re.search(r"(?<![`\w])`[^`\n]+`(?![`\w])", text):
        return True
    return False


def parse_markdown(text: str) -> List[Run]:
    lines = text.split("\n")
    spans: List[Run] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            index += 1
            code: List[str] = []
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            spans.append(("\n".join(code), PRE))
            if index < len(lines):
                spans.append(("\n", 0))
            continue
        flags = 0
        content = line
        if content.startswith("> "):
            flags |= QUOTE
            content = content[2:]
        heading = re.match(r"^#{1,6} (.+)$", content)
        if heading:
            flags |= BOLD
            content = heading.group(1)
        spans.extend(_parse_inline(content, flags))
        if index < len(lines) - 1:
            spans.append(("\n", 0))
        index += 1
    return spans


def _runs(
    text: str, entities: List[Dict[str, Any]], markdown: bool = True
) -> List[Run]:
    if _has_formatting(entities):
        return _runs_from_entities(text, entities)
    if markdown and looks_like_markdown(text):
        return parse_markdown(text)
    return _runs_from_entities(text, entities)


def _has_formatting(entities: Iterable[Dict[str, Any]]) -> bool:
    for entity in entities:
        if _entity_bit(entity) & _FORMATTING_BITS:
            return True
    return False


def _runs_from_entities(text: str, entities: List[Dict[str, Any]]) -> List[Run]:
    masks = [0] * len(text)
    for entity in entities:
        bit = _entity_bit(entity)
        if not bit:
            continue
        start = int(entity.get("offset") or 0)
        length = int(entity.get("length") or 0)
        end = start + length
        if start < 0 or length <= 0 or start >= len(text):
            continue
        end = min(end, len(text))
        for index in range(start, end):
            masks[index] |= bit
    if not text:
        return []
    runs: List[Run] = []
    current = text[0]
    mask = masks[0]
    for index in range(1, len(text)):
        if masks[index] == mask:
            current += text[index]
            continue
        runs.append((current, mask))
        current = text[index]
        mask = masks[index]
    runs.append((current, mask))
    return runs


def _entity_bit(entity: Dict[str, Any]) -> int:
    kind = (entity.get("type") or {}).get("@type", "")
    return _ENTITY_BITS.get(kind, 0)


def _parse_inline(text: str, flags: int) -> List[Run]:
    spans: List[Run] = []
    position = 0
    while position < len(text):
        match = _next_marker(text, position)
        if match is None:
            spans.append((text[position:], flags))
            break
        start, end, inner, bit = match
        if start > position:
            spans.append((text[position:start], flags))
        if inner:
            spans.append((inner, flags | bit))
        position = end
    return [span for span in spans if span[0]]


def _next_marker(
    text: str, position: int
) -> Optional[Tuple[int, int, str, int]]:
    found: Optional[Tuple[int, int, str, int]] = None
    for pattern, bit in (
        (r"`([^`\n]+)`", CODE),
        (r"\*\*([^\*\n]+)\*\*", BOLD),
        (r"~~([^~\n]+)~~", STRIKE),
        (r"\[([^\]]+)\]\([^)]+\)", LINK),
        (r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", ITALIC),
    ):
        match = re.search(pattern, text[position:])
        if match is None:
            continue
        start = position + match.start()
        end = position + match.end()
        if found is None or start < found[0]:
            found = (start, end, match.group(1), bit)
    return found


def _wrap(runs: List[Run], width: int) -> List[Line]:
    lines: List[Line] = []
    current: Line = []
    column = 0

    def break_line() -> None:
        nonlocal current, column
        lines.append(current)
        current = []
        column = 0

    for token, mask in _tokens(runs):
        if token == "\n":
            break_line()
            continue
        if token.isspace():
            if column == 0 or column >= width:
                continue
            current.append((token, _styles(mask)))
            column += string_len_dwc(token)
            continue
        remaining = token
        while remaining:
            room = width - column
            if room <= 0:
                break_line()
                room = width
            piece_width = string_len_dwc(remaining)
            if piece_width <= room:
                current.append((remaining, _styles(mask)))
                column += piece_width
                break
            if column > 0:
                break_line()
                continue
            pieces = split_string_dwc(remaining, width)
            for piece in pieces[:-1]:
                lines.append([(piece, _styles(mask))])
            remaining = pieces[-1] if pieces else ""
            column = 0
            if remaining and string_len_dwc(remaining) <= width:
                current.append((remaining, _styles(mask)))
                column = string_len_dwc(remaining)
                break
    if current:
        lines.append(current)
    return lines


def _tokens(runs: List[Run]) -> Iterable[Run]:
    for text, mask in runs:
        word = ""
        for char in text:
            if char == "\n":
                if word:
                    yield word, mask
                    word = ""
                yield "\n", mask
            elif char.isspace():
                if word:
                    yield word, mask
                    word = ""
                yield char, mask
            else:
                word += char
        if word:
            yield word, mask


def _styles(mask: int) -> FrozenSet[str]:
    return frozenset(name for name, bit in _STYLE_BITS if mask & bit)


def _collapse_blank_lines(lines: List[Line]) -> List[Line]:
    collapsed: List[Line] = []
    for line in lines:
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)
    while collapsed and not collapsed[0]:
        collapsed.pop(0)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return collapsed
