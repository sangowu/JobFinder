"""从 CV 纯文本中提取结构化 CVProfile。"""
from __future__ import annotations

import hashlib

from jobfinder import cache
from jobfinder.llm_backend import DEFAULT_MODELS, LLMConfig, Provider, complete_structured
from jobfinder.logger import get_logger
from jobfinder.schemas import CVProfile

logger = get_logger(__name__)

_SYSTEM = """你是一名专业的简历解析助手。
从用户提供的简历文本中提取关键信息，填入指定的 JSON 结构。

规则：
- summary：一句话概括候选人的专业定位，如 "3年经验的全栈工程师，擅长 React + Python"，不包含姓名
- skills：提取技术技能，去除软技能（如"团队合作"）
- preferred_locations：提取候选人明确表达的工作地点偏好，统一为英文地名
- years_of_experience：估算总工作年限，无法判断填 0
- seniority：根据工作年限和职位级别判断：
    在读/实习            → "intern"
    0年/应届/刚毕业      → "new_grad"
    <3年                → "junior"
    3-6年               → "mid"
    6+年                → "senior"
    管理岗/tech lead     → "lead"
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
        cached = cache.get_cv_profile(cv_hash)
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
    cache.save_cv_profile(cv_hash, profile)
    logger.info("CV 解析完成，结果已缓存（hash=%s）", cv_hash[:12])
    return profile
