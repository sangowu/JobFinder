"""Centralized seniority normalization and policy helpers."""
from __future__ import annotations

import re
from typing import Literal

LEGACY_SENIORITY_LEVEL = Literal["intern", "new_grad", "junior", "mid", "senior", "lead", "unknown"]

_TITLE_SPLIT_RE = re.compile(r"[\s/\-,|@().:+]+")

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lead", ("vice president", "vp", "head of", "director", "distinguished", "fellow", "cto", "cio", "cso")),
    ("lead", ("principal", "architect", "staff", "manager", "engineering manager", "tech lead", "team lead", "lead staff")),
    ("senior", ("senior", "sr", "technical lead", "supervisor")),
    ("mid", ("mid", "mid-level", "mid level", "intermediate", "experienced", "ii", "iii")),
    ("junior", ("junior", "jr", "entry level", "entry-level", "entry", "associate", "assistant")),
    ("new_grad", ("new grad", "new graduate", "graduate program", "graduate programme", "graduate", "recent graduate")),
    ("intern", ("intern", "internship", "placement", "apprentice", "trainee")),
)

_ALIASES = {
    "graduate": "new_grad",
    "entry": "junior",
    "entry_level": "junior",
    "associate": "junior",
    "staff": "lead",
    "principal": "lead",
    "manager": "lead",
    "director": "lead",
}

_LEVEL_RANK = {
    "intern": 0,
    "new_grad": 1,
    "junior": 2,
    "mid": 3,
    "senior": 4,
    "lead": 5,
}


def normalize_text(value: str) -> str:
    return value.strip().lower()


def normalize_seniority_level(level: str) -> LEGACY_SENIORITY_LEVEL:
    raw = normalize_text(level) if level else "unknown"
    raw = _ALIASES.get(raw, raw)
    if raw in {"intern", "new_grad", "junior", "mid", "senior", "lead"}:
        return raw
    return "unknown"


def normalize_seniority(raw_values: list[str], fallback: str = "unknown") -> LEGACY_SENIORITY_LEVEL:
    best_match = "unknown"
    best_rank = -1

    for raw_value in raw_values:
        text = normalize_text(raw_value)
        if not text:
            continue
        for level, keywords in _KEYWORDS:
            if any(keyword in text for keyword in keywords):
                rank = _LEVEL_RANK[level]
                if rank > best_rank:
                    best_match = level
                    best_rank = rank

    if best_match != "unknown":
        return best_match  # type: ignore[return-value]

    fallback_level = normalize_seniority_level(fallback)
    if fallback_level != "unknown":
        return fallback_level

    fallback_text = normalize_text(fallback)
    for level, keywords in _KEYWORDS:
        if any(keyword in fallback_text for keyword in keywords):
            return level  # type: ignore[return-value]

    return "unknown"


def infer_title_seniority(title: str) -> LEGACY_SENIORITY_LEVEL:
    text = normalize_text(title)
    if not text:
        return "unknown"

    for level, keywords in _KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return level  # type: ignore[return-value]

    tokens = {token for token in _TITLE_SPLIT_RE.split(text) if token}
    if "iv" in tokens or "v" in tokens:
        return "lead"
    return "unknown"


def seniority_rank(level: str) -> int | None:
    normalized = normalize_seniority_level(level)
    return _LEVEL_RANK.get(normalized)


def default_eligible_levels(level: LEGACY_SENIORITY_LEVEL) -> list[str]:
    defaults = {
        "intern": ["intern"],
        "new_grad": ["intern", "new_grad", "junior"],
        "junior": ["new_grad", "junior", "mid"],
        "mid": ["junior", "mid", "senior"],
        "senior": ["mid", "senior", "lead"],
        "lead": ["senior", "lead"],
        "unknown": ["new_grad", "junior", "mid"],
    }
    return defaults[level]


def default_stretch_levels(level: LEGACY_SENIORITY_LEVEL, mode: str) -> list[str]:
    if mode == "strict":
        return []
    defaults = {
        "intern": ["new_grad"] if mode == "stretch" else [],
        "new_grad": ["mid"] if mode == "stretch" else ["junior"],
        "junior": ["senior"] if mode == "stretch" else ["mid"],
        "mid": ["lead"] if mode == "stretch" else ["senior"],
        "senior": ["lead"] if mode == "stretch" else ["lead"],
        "lead": [] if mode == "balanced" else [],
        "unknown": ["senior"] if mode == "stretch" else ["mid"],
    }
    return defaults[level]


def default_blocked_levels(level: LEGACY_SENIORITY_LEVEL) -> list[str]:
    defaults = {
        "intern": ["junior", "mid", "senior", "lead"],
        "new_grad": ["senior", "lead"],
        "junior": ["lead"],
        "mid": ["lead"],
        "senior": [],
        "lead": [],
        "unknown": ["lead"],
    }
    return defaults[level]
