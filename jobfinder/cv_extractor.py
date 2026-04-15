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
- skills：提取技术技能，去除软技能（如"团队合作"）
- preferred_locations：提取候选人明确表达的工作地点偏好，统一为英文地名
- preferred_roles：
    【不要提取 CV 中写的职位名，而是主动生成】
    分析候选人的技能栈、项目经历、学历背景，结合当前招聘市场上真实存在的职位名称，
    生成该候选人最可能成功申请的 4-8 个职位 title，要求：
    1. 覆盖"精准匹配"到"宽泛兜底"的梯度，例如：
       精准：AI Engineer / ML Engineer
       变体：Machine Learning Engineer / Applied AI Engineer
       宽泛：Software Engineer / Data Scientist
    2. 只使用市场上招聘广告中真实出现的职位名称
    3. 全部为英文，不加说明文字
    4. 根据 seniority 调整：new_grad 不加 Senior/Staff 前缀
- years_of_experience：估算总工作年限，无法判断填 0
- seniority：根据工作年限和职位级别判断：
    在读/实习            → "intern"
    0年/应届/刚毕业      → "new_grad"
    <3年                → "junior"
    3-6年               → "mid"
    6+年                → "senior"
    管理岗/tech lead     → "lead"
- search_language：根据目标市场判断搜索语言：
    英语市场（欧美澳）→ "en"
    中文市场（中国大陆/港台）→ "zh"
    其他市场 → 对应语言代码
- search_terms：根据 seniority + preferred_locations 生成 2-4 个适合当地市场的职位搜索术语。
    new_grad + 爱尔兰/英国/欧洲 → ["graduate programme", "graduate engineer", "entry level"]
    new_grad + 美国/加拿大      → ["new grad", "entry level", "university grad"]
    new_grad + 澳大利亚         → ["graduate program", "entry level", "associate"]
    new_grad + 中国             → ["应届生", "校招", "实习转正"]
    intern                      → ["internship", "intern", "placement"]
    junior                      → ["junior", "associate", "1-3 years experience"]
    mid                         → ["mid-level", "intermediate", "3-5 years experience"]
    senior                      → ["senior", "staff", "6+ years experience"]
    lead                        → ["lead", "principal", "tech lead", "engineering manager"]
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
