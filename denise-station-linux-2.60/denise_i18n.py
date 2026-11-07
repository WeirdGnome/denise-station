"""Tiny gettext-style internationalisation for Denise Station.

Reads plain .po files from ./locale at runtime (no msgfmt/.mo build step needed).
Usage:  from denise_i18n import _, set_language
Translatable strings must be literal first arguments of _(...), with {named}
placeholders for variables, so translators can reorder them.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "locale"

# Native names shown in the language picker. A language is offered only if its
# locale/<code>.po file exists (English is built in).
LANGUAGE_NAMES = {"en": "English", "pl": "Polski", "de": "Deutsch",
                  "fr": "Français", "es": "Español"}

_catalog: dict[str, str] = {}
_current = "en"


def _unescape(s: str) -> str:
    return re.sub(r'\\(["\\nt])', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def parse_po(text: str) -> dict[str, str]:
    """Minimal .po parser. Entries are blocks separated by blank lines; fuzzy ones are skipped."""
    out: dict[str, str] = {}
    for block in re.split(r"\n\s*\n", text):
        msgid = msgstr = None
        field = None
        fuzzy = False
        for raw in block.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                fuzzy = fuzzy or (line.startswith("#,") and "fuzzy" in line)
            elif line.startswith("msgid "):
                msgid, field = _unescape(line[6:].strip()[1:-1]), "id"
            elif line.startswith("msgstr "):
                msgstr, field = _unescape(line[7:].strip()[1:-1]), "str"
            elif line.startswith('"') and line.endswith('"') and len(line) >= 2:
                piece = _unescape(line[1:-1])
                if field == "id" and msgid is not None:
                    msgid += piece
                elif field == "str" and msgstr is not None:
                    msgstr += piece
        if msgid and msgstr and not fuzzy:
            out[msgid] = msgstr
    return out


def available_languages() -> dict[str, str]:
    langs = {"en": LANGUAGE_NAMES["en"]}
    for code, name in LANGUAGE_NAMES.items():
        if (LOCALE_DIR / f"{code}.po").exists():
            langs[code] = name
    return langs


def _system_locales() -> list[str]:
    """Locale names of the desktop, most specific first: 'pl_PL', 'pl', ..."""
    cands: list[str] = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        cands += os.environ.get(var, "").split(":")
    try:
        import locale
        cands.append((locale.getlocale()[0] or ""))
    except (ValueError, ImportError):
        pass
    return cands


def detect_language() -> str:
    """Best match for the desktop locale among the available languages."""
    avail = available_languages()
    for cand in _system_locales():
        cand = re.split(r"[.@]", cand)[0]
        for code in (cand, cand.split("_")[0]):
            if code in avail:
                return code
    return "en"


def set_language(code: str = "auto") -> str:
    """Activate a language ('auto' follows the system). Returns the code actually used."""
    global _catalog, _current
    if not code or code == "auto":
        code = detect_language()
    if code not in available_languages():
        code = "en"
    _catalog = {}
    if code != "en":
        try:
            _catalog = parse_po((LOCALE_DIR / f"{code}.po").read_text(encoding="utf-8"))
        except OSError:
            code = "en"
    _current = code
    return code


def current_language() -> str:
    return _current


def _(msgid: str) -> str:
    return _catalog.get(msgid) or msgid
