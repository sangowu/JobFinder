"""职位标题过滤工具：年资匹配过滤。"""
from __future__ import annotations

import re

# intern/new_grad：跳过 mid 及以上（II+）
_SKIP_IF_NEWGRAD: frozenset[str] = frozenset({
    "senior", "sr", "staff", "lead", "principal", "director", "head",
    "vp", "vice", "president", "distinguished", "fellow", "manager",
    "architect", "cto", "cio", "cso", "founding",
    "ii", "iii", "iv", "v",
})

# junior：可看到 mid（II），跳过 senior 及以上（III+）
_SKIP_IF_JUNIOR: frozenset[str] = frozenset({
    "senior", "sr", "staff", "lead", "principal", "director", "head",
    "vp", "vice", "president", "distinguished", "fellow", "manager",
    "architect", "cto", "cio", "cso", "founding",
    "iii", "iv", "v",
})

# mid：可看到 senior（III），跳过 staff 及以上（IV+）和实习词（I）
_SKIP_IF_MID_HIGH: frozenset[str] = frozenset({
    "staff", "principal", "director", "vp", "vice", "head",
    "distinguished", "fellow", "cto", "cio", "cso",
    "iv", "v",
})
_SKIP_IF_MID_LOW: frozenset[str] = frozenset({
    "intern", "internship", "placement", "apprentice", "trainee",
    "i",
})

# senior/lead：跳过 junior/mid（I/II）及实习词
_SKIP_IF_HIGH: frozenset[str] = frozenset({
    "intern", "internship", "placement", "apprentice", "trainee",
    "junior", "jr", "associate", "entry",
    "i", "ii",
})
_SKIP_IF_MID_LOW_PHRASES: tuple[str, ...] = (
    "graduate programme", "graduate program", "entry level",
)


def is_seniority_ok(title: str, seniority: str) -> bool:
    """
    根据候选人年资判断 title 是否应该处理。
    返回 False 表示该 title 明显不符合年资，应跳过。
    """
    t = title.lower()
    tokens = set(re.split(r"[\s/\-,|@().]+", t))  # 含 . 避免 "Sr." 漏过

    if seniority in ("new_grad", "intern"):
        return not bool(tokens & _SKIP_IF_NEWGRAD)

    if seniority == "junior":
        return not bool(tokens & _SKIP_IF_JUNIOR)

    if seniority == "mid":
        if tokens & _SKIP_IF_MID_HIGH:
            return False
        if tokens & _SKIP_IF_MID_LOW:
            return False
        if any(phrase in t for phrase in _SKIP_IF_MID_LOW_PHRASES):
            return False
        return True

    if seniority in ("senior", "lead"):
        return not bool(tokens & _SKIP_IF_HIGH)

    return True  # unknown 不过滤
