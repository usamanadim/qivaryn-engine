"""Small HTML/JSON scraping helpers shared by the native platform clients.

These utilities exist because every one of the supported services embeds its
state as JSON inside HTML, but each one does it differently: YouTube assigns a
JavaScript variable, TikTok and Facebook use ``<script>`` tags, and Facebook
further escapes URLs inside its JSON strings.  Keeping the parsing logic here
avoids four near-identical copies drifting apart.
"""

import json
import re
from html import unescape as html_unescape
from typing import Any, Dict, Iterator, List, Optional


def unescape_js(value: str) -> str:
    """Decodes the escape sequences services use inside embedded JSON strings.

    Facebook in particular emits ``\\u0025`` for ``%`` and ``\\/`` for ``/``,
    which makes raw regex extraction produce unusable URLs.
    """
    if not value:
        return value
    try:
        return json.loads(f'"{value}"')
    except (ValueError, TypeError):
        return (
            value.replace("\\/", "/")
            .replace("\\u0025", "%")
            .replace("\\u0026", "&")
            .replace("\\u003d", "=")
            .replace("\\u003f", "?")
        )


def extract_balanced_json(text: str, start: int) -> Optional[str]:
    """Returns the balanced ``{...}`` or ``[...]`` block beginning at ``start``."""
    if start < 0 or start >= len(text) or text[start] not in "{[":
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def json_after(text: str, marker: str) -> Optional[Any]:
    """Parses the first JSON value that follows ``marker`` in ``text``."""
    index = text.find(marker)
    while index != -1:
        brace = -1
        for offset in range(index + len(marker), min(len(text), index + len(marker) + 200)):
            if text[offset] in "{[":
                brace = offset
                break
            if not text[offset].isspace() and text[offset] not in "=:(":
                break
        if brace != -1:
            raw = extract_balanced_json(text, brace)
            if raw:
                try:
                    return json.loads(raw)
                except ValueError:
                    pass
        index = text.find(marker, index + 1)
    return None


def script_json(html: str, script_id: str) -> Optional[Any]:
    """Parses the JSON body of ``<script id="...">`` blocks."""
    pattern = re.compile(
        r'<script[^>]*id=["\']' + re.escape(script_id) + r'["\'][^>]*>(.*?)</script>',
        re.DOTALL,
    )
    for match in pattern.finditer(html):
        payload = match.group(1).strip()
        if not payload:
            continue
        try:
            return json.loads(payload)
        except ValueError:
            continue
    return None


def iter_keyed_objects(data: Any, key: str) -> Iterator[Dict[str, Any]]:
    """Yields every nested dictionary stored under ``key``."""
    stack: List[Any] = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            value = current.get(key)
            if isinstance(value, dict):
                yield value
            elif isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        yield entry
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def first_matching(pattern: str, text: str, group: int = 1, flags: int = 0) -> Optional[str]:
    """Returns the first capture group of ``pattern`` found in ``text``."""
    match = re.search(pattern, text, flags)
    if not match:
        return None
    return match.group(group)


def meta_content(html: str, prop: str) -> Optional[str]:
    """Reads a ``<meta>`` tag's ``content`` attribute by ``property`` or ``name``."""
    pattern = (
        r'<meta[^>]+(?:property|name)=["\']'
        + re.escape(prop)
        + r'["\'][^>]*content=["\']([^"\']*)["\']'
    )
    found = first_matching(pattern, html, flags=re.IGNORECASE)
    if found is None:
        reverse = (
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']'
            + re.escape(prop)
            + r'["\']'
        )
        found = first_matching(reverse, html, flags=re.IGNORECASE)
    return html_unescape(unescape_js(found)) if found else None


def unique_preserving_order(values: Iterator[str]) -> List[str]:
    """Removes duplicates while keeping the original ordering."""
    seen = set()
    result: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
