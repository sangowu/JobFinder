"""Backward-compatible wrappers around the JDProfile extraction layer."""
from __future__ import annotations

from jobradar.jd_profile import extract_jd_profile, jd_profile_prompt_version


def summary_prompt_version(language: str) -> str:
    return jd_profile_prompt_version(language)


def summarize_jd(job, llm, language: str = "zh"):
    return extract_jd_profile(job, llm, language=language)
