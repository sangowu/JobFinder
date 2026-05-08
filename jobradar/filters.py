"""Title seniority filtering helpers."""
from __future__ import annotations

from jobradar.schemas import CVProfile
from jobradar.seniority import infer_title_seniority, normalize_seniority_level, seniority_rank

TITLE_GATE_VERSION = "title_gate_v1"


def is_title_seniority_ok(title: str, profile: CVProfile) -> bool:
    inferred = normalize_seniority_level(infer_title_seniority(title))
    if inferred == "unknown":
        return True

    eligible = {normalize_seniority_level(item) for item in profile.eligible_seniority_levels}
    stretch = {normalize_seniority_level(item) for item in profile.stretch_seniority_levels}
    blocked = {normalize_seniority_level(item) for item in profile.blocked_seniority_levels}

    if inferred in blocked:
        return False
    if inferred in eligible or inferred in stretch:
        return True
    inferred_rank = seniority_rank(inferred)
    allowed_ranks = [seniority_rank(level) for level in {*eligible, *stretch}]
    allowed_ranks = [rank for rank in allowed_ranks if rank is not None]
    if inferred_rank is None or not allowed_ranks:
        return True
    return min(allowed_ranks) <= inferred_rank <= max(allowed_ranks)


def is_seniority_ok(title: str, seniority: str) -> bool:
    """Legacy wrapper kept for compatibility with older call sites."""
    profile = CVProfile(
        summary="",
        skills=[],
        seniority=seniority or "unknown",
    )
    return is_title_seniority_ok(title, profile)
