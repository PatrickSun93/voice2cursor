r"""Canonical spellings for site jargon Whisper has never heard.

`initial_prompt` biases the decoder toward a vocabulary, but it is capped at
224 tokens - roughly 60 terms - and it only ever biases. A site with hundreds
of panel names and daemon identifiers needs something with no size limit and a
predictable result, so the transcript is swept for known terms after decoding
as well.

Every entry is the CORRECT spelling. Matching ignores case and any spaces,
hyphens or underscores between word parts, so the single entry "TempleCity"
also catches "temple city", "Temple-City" and "templecity". Wrong spellings
only need listing in `aliases`, for the cases where what Whisper heard shares
no letters with the right word ("zyphen" -> "Xifin").
"""
from __future__ import annotations

import json
import os
import re

_SPLIT = re.compile(r"[\s_-]+")
_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STRIP = re.compile(r"[\s_-]+")
_SEP = r"[\s_-]*"


def _parts(term: str) -> list[str]:
    """Word parts of a term, including the ones camel case only implies.

    Splitting "NodeFlims" at the hump is what lets it match the "node flims"
    Whisper actually writes, since nothing in the audio marks the boundary.
    """
    out: list[str] = []
    for chunk in _SPLIT.split(term):
        out += [p for p in _HUMP.split(chunk) if p]
    return out


def _key(text: str) -> str:
    return _STRIP.sub("", text).lower()


class Vocabulary:
    def __init__(self, terms: list[str], aliases: dict[str, str]):
        pairs = [(t, t) for t in terms if t] + [
            (heard, right) for heard, right in aliases.items() if heard and right
        ]
        # Longest first: Python alternation takes the first branch that matches,
        # not the longest, so "NGS post-processing" has to precede "NGS".
        pairs.sort(key=lambda p: len(p[0]), reverse=True)

        self._canon = {_key(src): right for src, right in pairs}
        alts = [_SEP.join(re.escape(p) for p in _parts(src)) for src, _ in pairs if _parts(src)]
        # The guards exclude `_` as well as alphanumerics, so the entry "eFax"
        # cannot rewrite the middle of the daemon name "dmn_efax".
        self._re = (
            re.compile(
                r"(?<![A-Za-z0-9_])(?:" + "|".join(alts) + r")(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            if alts
            else None
        )

    def __len__(self) -> int:
        return len(self._canon)

    def apply(self, text: str) -> str:
        if not text or self._re is None:
            return text
        return self._re.sub(
            lambda m: self._canon.get(_key(m.group(0)), m.group(0)), text
        )

    @classmethod
    def load(cls, path: str) -> "Vocabulary":
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, list):  # a bare list of terms is accepted too
            return cls(doc, {})
        return cls(doc.get("terms") or [], doc.get("aliases") or {})


_cache: tuple[str, int, Vocabulary] | None = None


def load_cached(path: str) -> Vocabulary | None:
    """Load the file, rebuilding only when it changes on disk.

    Compiling ~900 alternatives costs enough to be worth doing once, but the
    file is meant to be hand-edited, so the mtime is checked every time and an
    edit takes effect on the next dictation - no restart, no menu action.
    """
    global _cache
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None  # no vocabulary file is a normal, silent state
    if _cache and _cache[0] == path and _cache[1] == stamp:
        return _cache[2]
    try:
        vocab = Vocabulary.load(path)
    except (OSError, ValueError):
        return None  # a malformed edit should cost corrections, not the transcript
    _cache = (path, stamp, vocab)
    return vocab
