"""Title seniority filtering helpers."""
from __future__ import annotations

import re

from jobradar.schemas import CVProfile

_TITLE_SPLIT_RE = re.compile(r"[\s/\-,|@().:+]+")

_PHRASE_LEVELS: tuple[tuple[str, str], ...] = (
    ("vice president", "director"),
    ("head of", "director"),
    ("engineering manager", "manager"),
    ("tech lead", "lead"),
    ("team lead", "lead"),
    ("new grad", "new_grad"),
    ("new graduate", "new_grad"),
    ("entry level", "junior"),
    ("graduate program", "new_grad"),
    ("graduate programme", "new_grad"),
)

_TOKEN_LEVELS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"vp", "president", "director", "head", "distinguished", "fellow", "cto", "cio", "cso"}), "director"),
    (frozenset({"manager"}), "manager"),
    (frozenset({"principal"}), "principal"),
    (frozenset({"staff"}), "staff"),
    (frozenset({"lead", "architect", "founding"}), "lead"),
    (frozenset({"senior", "sr"}), "senior"),
    (frozenset({"iii"}), "senior"),
    (frozenset({"ii"}), "mid"),
    (frozenset({"mid"}), "mid"),
    (frozenset({"junior", "jr", "associate", "entry"}), "junior"),
    (frozenset({"graduate"}), "new_grad"),
    (frozenset({"intern", "internship", "placement", "apprentice", "trainee"}), "intern"),
)

_LEVEL_ALIASES = {
    "graduate": "new_grad",
    "entry": "junior",
    "entry_level": "junior",
    "associate": "junior",
}

_LEVEL_RANK = {
    "intern": 0,
    "new_grad": 1,
    "junior": 2,
    "mid": 3,
    "senior": 4,
    "lead": 5,
    "staff": 6,
    "principal": 6,
    "manager": 6,
    "director": 7,
}


def _normalize_level(level: str) -> str:
    raw = (level or "").strip().lower()
    return _LEVEL_ALIASES.get(raw, raw)


def infer_title_seniority(title: str) -> str:
    text = (title or "").strip().lower()
    if not text:
        return "unknown"

    for phrase, level in _PHRASE_LEVELS:
        if phrase in text:
            return level

    tokens = {token for token in _TITLE_SPLIT_RE.split(text) if token}
    if "iv" in tokens or "v" in tokens:
        return "lead"

    for bucket, level in _TOKEN_LEVELS:
        if tokens & bucket:
            return level
    return "unknown"


def is_title_seniority_ok(title: str, profile: CVProfile) -> bool:
    inferred = _normalize_level(infer_title_seniority(title))
    if inferred == "unknown":
        return True

    eligible = {_normalize_level(item) for item in profile.eligible_seniority_levels}
    stretch = {_normalize_level(item) for item in profile.stretch_seniority_levels}
    blocked = {_normalize_level(item) for item in profile.blocked_seniority_levels}

    if inferred in blocked:
        return False
    if inferred in eligible or inferred in stretch:
        return True
    inferred_rank = _LEVEL_RANK.get(inferred)
    allowed_ranks = [_LEVEL_RANK[level] for level in {*eligible, *stretch} if level in _LEVEL_RANK]
    if inferred_rank is None or not allowed_ranks:
        return True
    return inferred_rank <= max(allowed_ranks)


def is_seniority_ok(title: str, seniority: str) -> bool:
    """Legacy wrapper kept for compatibility with older call sites."""
    profile = CVProfile(
        summary="",
        skills=[],
        seniority=seniority or "unknown",
    )
    return is_title_seniority_ok(title, profile)
