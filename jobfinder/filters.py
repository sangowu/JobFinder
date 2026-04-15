"""职位标题过滤工具：年资匹配过滤。"""
from __future__ import annotations

import re

# 高级职位词（new_grad / intern / junior 跳过）
_SKIP_IF_LOW: frozenset[str] = frozenset({
    "senior", "sr", "staff", "lead", "principal", "director", "head",
    "vp", "vice", "president", "distinguished", "fellow", "manager",
    "architect", "cto", "cio", "cso", "founding",
})

# 极高级词（mid 跳过，不含 senior/lead）
_SKIP_IF_MID_HIGH: frozenset[str] = frozenset({
    "staff", "principal", "director", "vp", "vice", "head",
    "distinguished", "fellow", "cto", "cio", "cso",
})

# 初级/实习专项词（senior / lead 跳过）
_SKIP_IF_HIGH: frozenset[str] = frozenset({
    "intern", "internship", "placement", "apprentice", "trainee",
    "junior", "jr", "associate", "entry",
})

# mid 跳过的初级词（短语整体匹配）
_SKIP_IF_MID_LOW: frozenset[str] = frozenset({
    "intern", "internship", "placement", "apprentice", "trainee",
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

    if seniority in ("new_grad", "intern", "junior"):
        return not bool(tokens & _SKIP_IF_LOW)

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
