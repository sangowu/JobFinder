"""从 CV 纯文本中提取结构化 CVProfile。"""
from __future__ import annotations

import hashlib

from jobradar import cache
from jobradar.llm_backend import DEFAULT_MODELS, LLMConfig, Provider, complete_structured
from jobradar.logger import get_logger
from jobradar.schemas import CVProfile

logger = get_logger(__name__)
PROMPT_VERSION = "cv_extract_v2"

_SYSTEM = """你是一名专业的简历解析助手。
从用户提供的简历文本中提取关键信息，填入指定的 JSON 结构。

规则：
- summary：一句话概括候选人的专业定位，如 "3年经验的全栈工程师，擅长 React + Python"，不包含姓名
- skills：提取技术技能，去除软技能（如"团队合作"）
- languages：提取候选人在 CV 中明确写出的语言能力，输出为 [{name, level}]；例如 English/C1、Mandarin/Native。不要臆测没写出的语言。
- preferred_locations：提取候选人明确表达的工作地点偏好，统一为英文地名
- years_of_experience：估算总工作年限，无法判断填 0
- seniority 相关字段必须一起输出：
    - seniority：兼容旧字段，填最终综合判断后的主级别，仅允许
      "intern" / "new_grad" / "junior" / "mid" / "senior" / "lead" / "unknown"
    - declared_seniority：候选人在 CV 中呈现出的资历级别
    - evidence_seniority：基于项目复杂度、生产经验、ownership、领导职责、领域相关性、教育背景综合推断出的资历级别
    - eligible_seniority_levels：正常可投递级别
    - stretch_seniority_levels：可尝试但需要提示风险的级别
    - blocked_seniority_levels：明显不建议投递的级别
    - reasoning_summary：用一句话解释 seniority 判断
- 不要只根据工作年限判断 seniority。必须同时考虑：
    - project complexity
    - production experience
    - ownership level
    - leadership responsibility
    - domain relevance
    - academic background
    - user preferred roles
- seniority_mode 默认填 "balanced"
- preferred_roles：
    【不要照抄 CV 中写的职位名，而是主动推断】
    分析候选人的技能栈、项目经历、学历背景，结合当前招聘市场上真实存在的职位名称，
    生成该候选人最可能成功申请的 4-8 个职位 title，要求：
    1. 覆盖"精准"到"宽泛"的梯度，每层选互不重叠的词，例如：
       精准：AI Engineer、ML Engineer
       宽泛：Software Engineer、Data Scientist
       【不要加"变体"层】——"Applied AI Engineer"的搜索结果已被"AI Engineer"覆盖，列出反而产生重复查询
    2. 只使用市场上招聘广告中真实出现的职位名称
    3. 全部为英文，不加说明文字
    4. intern/new_grad 不加 Senior/Staff/Lead 前缀；senior/lead 不加 Junior/Associate 前缀
- search_language：根据目标市场判断搜索语言：
    英语市场（欧美澳）→ "en"
    中文市场（中国大陆/港台）→ "zh"
    其他市场 → 对应语言代码
- search_terms：根据 seniority + preferred_locations 生成 2-4 个适合当地招聘市场的搜索修饰词（非职位名）。
    new_grad + 英语市场（英/美/加/澳/爱尔兰/欧洲英语国家）→ 结合目标国家惯用语自行选取，候选词池：
        ["graduate programme", "graduate program", "new grad", "university grad", "entry level", "associate"]
    new_grad + 中国             → ["应届生", "校招", "实习转正"]
    intern                      → ["internship", "intern", "placement"]
    junior                      → ["junior", "associate"]
    mid                         → ["mid-level", "intermediate"]
    senior                      → ["senior", "staff"]
    lead                        → ["lead", "principal", "tech lead", "manager"]
- 如果 declared_seniority 与 evidence_seniority 不一致，不要强行改成一致；保留差异并通过 eligible/stretch/blocked 表达可投范围。
"""


def extract_cv_profile(
    cv_text: str,
    llm: LLMConfig | None = None,
    use_cache: bool = True,
    # 兼容旧参数
    provider: Provider = "claude",
    model: str | None = None,
) -> CVProfile:
    """将 CV 纯文本发给 LLM，返回结构化 CVProfile。CV 内容不变时直接返回缓存结果。"""
    effective_llm = llm or LLMConfig(
        provider=provider,
        model=model or DEFAULT_MODELS[provider],
    )
    cv_hash = hashlib.sha256(cv_text.encode()).hexdigest()

    if use_cache:
        cached = cache.get_cv_profile(cv_hash, prompt_version=PROMPT_VERSION)
        if cached is not None:
            logger.info("CV parse cache hit (hash=%s)", cv_hash[:12])
            return cached

    logger.info("Starting CV parse (LLM) provider=%s model=%s", effective_llm.provider, effective_llm.model)
    prompt = f"请从以下简历中提取信息：\n\n{cv_text}"
    profile = complete_structured(
        prompt=prompt,
        response_schema=CVProfile,
        provider=effective_llm.provider,
        model=effective_llm.model,
        system=_SYSTEM,
        _step="CV 解析",
    )
    cache.save_cv_profile(cv_hash, profile, prompt_version=PROMPT_VERSION)
    logger.info("CV 解析完成，结果已缓存（hash=%s）", cv_hash[:12])
    return profile
