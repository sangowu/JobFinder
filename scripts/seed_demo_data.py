#!/usr/bin/env python3
"""
为演示/面试生成模拟的职位数据（带完整评估）。
运行后填充 jobfinder_test_cache.db，可直接在 Web UI 展示。

用法：
    python scripts/seed_demo_data.py
    uv run jobfinder serve --mock
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# ─── 演示数据：15 个真实公司职位 ───────────────────────────────────────────────

DEMO_JOBS = [
    {
        "title": "Senior AI/ML Engineer",
        "company": "OpenAI",
        "location": "San Francisco, CA",
        "url": "https://example.com/openai-ml-1",
        "description_snippet": "Lead research and development of transformer architectures. 8+ years ML experience required.",
        "score": 8,
        "strengths": [
            "深厚的 AI/ML 基础与 PyTorch 经验完美契合职位需求",
            "过去 3 个大模型项目经历直接适配 OpenAI 工程文化",
            "发表论文经历与研究导向的角色高度匹配",
        ],
        "weaknesses": [
            "对 RLHF 对齐技术的实战经验相对浅薄（仅课题研究阶段）",
            "未在 Transformer 扩展训练（10B+ 参数）的生产环节中操盘过",
        ],
        "matched_keywords": ["PyTorch", "Transformers", "CUDA", "Python", "Machine Learning", "Deep Learning", "Research"],
    },
    {
        "title": "Full Stack Engineer - AI Platform",
        "company": "Anthropic",
        "location": "San Francisco, CA",
        "url": "https://example.com/anthropic-fullstack-1",
        "description_snippet": "Build infrastructure for Claude. Python, TypeScript, cloud systems. 5-8 years exp.",
        "score": 7,
        "strengths": [
            "Python 与 TypeScript 全栈经验，系统设计能力达到 Senior 水准",
            "AWS/GCP 大规模基础设施项目经历，理解分布式系统容错设计",
            "FastAPI/Node.js 实战项目对标准工程流程熟悉",
        ],
        "weaknesses": [
            "缺少 CUDA/GPU 优化编程经验，需要快速上手 ML 工程侧工具链",
            "未直接参与 API 网关/限流系统的底层设计",
        ],
        "matched_keywords": ["Python", "TypeScript", "AWS", "FastAPI", "React", "System Design", "PostgreSQL"],
    },
    {
        "title": "Machine Learning Engineer - NLP",
        "company": "Google DeepMind",
        "location": "London, UK",
        "url": "https://example.com/deepmind-nlp-1",
        "description_snippet": "Research and develop next-generation language models. 6+ years NLP experience.",
        "score": 7,
        "strengths": [
            "NLP 领域 5+ 年深耕，BERT/GPT 系列模型的微调与蒸馏经验丰富",
            "多语言处理项目经历，对语言学约束的工程实现有深入理解",
            "论文发表与开源项目维护展现了研究驱动的思维模式",
        ],
        "weaknesses": [
            "对强化学习与 RLHF 的结合应用知识储备不足",
            "多模态（Vision-Language）方向触碰较少，学习曲线可能陡峭",
        ],
        "matched_keywords": ["NLP", "BERT", "Transformers", "Python", "PyTorch", "Multilingual", "Named Entity Recognition"],
    },
    {
        "title": "Backend Engineer - Payments",
        "company": "Stripe",
        "location": "San Francisco, CA",
        "url": "https://example.com/stripe-backend-1",
        "description_snippet": "Build reliable payment processing systems. 4-6 years backend exp required.",
        "score": 6,
        "strengths": [
            "3+ 年后端高并发系统开发，对分布式事务与一致性有扎实理解",
            "支付相关的反欺诈系统开发经验，风险控制思维成熟",
            "数据库性能优化与查询分析能力达到中等偏上水准",
        ],
        "weaknesses": [
            "未在金融级别的合规系统（PCI-DSS）中有过一线操作",
            "对微服务编排与 gRPC 通信的深度优化经验缺乏",
        ],
        "matched_keywords": ["Python", "Go", "PostgreSQL", "Redis", "Distributed Systems", "API Design", "Backend"],
    },
    {
        "title": "Staff Engineer - Infrastructure",
        "company": "Meta",
        "location": "Menlo Park, CA",
        "url": "https://example.com/meta-infra-1",
        "description_snippet": "Design and implement large-scale infrastructure. 8+ years exp, Staff level required.",
        "score": 5,
        "strengths": [
            "系统设计与架构治理能力达到 Senior 标准，有大型项目领导经历",
            "云原生技术栈（K8s/Terraform）实战与最佳实践理解深入",
            "性能调优与成本优化的系统化方法论清晰",
        ],
        "weaknesses": [
            "Staff 级别的战略规划与跨部门影响力相对欠缺（缺少集团级决策经验）",
            "对内部系统深度定制（如 Meta 自研中间件）的适应需要学习周期",
        ],
        "matched_keywords": ["System Design", "Kubernetes", "Terraform", "Python", "Go", "Cloud Infrastructure"],
    },
    {
        "title": "Product Engineer - AI Tools",
        "company": "Vercel",
        "location": "Remote",
        "url": "https://example.com/vercel-product-1",
        "description_snippet": "Build developer-centric AI products. Full stack experience. 3-5 years required.",
        "score": 8,
        "strengths": [
            "全栈能力与产品思维齐全，能从 0-1 完整交付功能模块",
            "React/Next.js 生态理解深，对开发者体验的感知敏锐",
            "API 设计与前后端协调效率高，原型迭代速度快",
        ],
        "weaknesses": [
            "对大规模数据持久化优化的需求理解可能不足（中小项目背景）",
        ],
        "matched_keywords": ["React", "Next.js", "TypeScript", "Vercel", "Tailwind CSS", "API Design"],
    },
    {
        "title": "Senior Data Engineer",
        "company": "Databricks",
        "location": "San Francisco, CA",
        "url": "https://example.com/databricks-de-1",
        "description_snippet": "Build scalable data pipelines. Spark, distributed systems. 5-7 years required.",
        "score": 7,
        "strengths": [
            "大数据处理与 Spark 生态 4+ 年深度使用，性能优化经验丰富",
            "数据仓库架构设计（Medallion 模式）与 ETL/ELT 流程清晰理解",
            "SQL 与 Python 双语言能力，可跨越数据与应用层无缝协作",
        ],
        "weaknesses": [
            "对流式处理（Kafka/Flink）的生产级部署经验相对短板",
            "ML Pipeline 集成（Model Registry、特征存储）的系统化理解不足",
        ],
        "matched_keywords": ["Apache Spark", "Python", "SQL", "Distributed Systems", "ETL", "Data Warehouse"],
    },
    {
        "title": "Frontend Engineer - Design Systems",
        "company": "GitHub",
        "location": "San Francisco, CA",
        "url": "https://example.com/github-frontend-1",
        "description_snippet": "Build GitHub's design system. 4+ years React/TypeScript, design collaboration.",
        "score": 6,
        "strengths": [
            "React 组件库设计与开源项目维护经验，对无障碍(A11y)有理解",
            "TypeScript 类型系统深度使用，组件 props 设计规范化",
            "Figma 与开发者沟通经验，能缩小设计-开发鸿沟",
        ],
        "weaknesses": [
            "对大规模组件库版本管理（semver、changelog）的策略经验缺乏",
            "性能监控与可视化回归测试工具链的理解深度不够",
        ],
        "matched_keywords": ["React", "TypeScript", "CSS", "Accessibility", "Design Systems", "Storybook"],
    },
    {
        "title": "Security Engineer - Cloud",
        "company": "Microsoft Azure",
        "location": "Redmond, WA",
        "url": "https://example.com/azure-security-1",
        "description_snippet": "Secure cloud infrastructure. 5+ years security + cloud exp. On-site position.",
        "score": 5,
        "strengths": [
            "云安全基础扎实（IAM、网络隔离、加密），AWS/GCP 环境操作熟悉",
            "安全审计与合规性工作经历（SOC 2、ISO 27001），流程理解清晰",
            "渗透测试与威胁建模的系统化方法有所掌握",
        ],
        "weaknesses": [
            "Azure 生态的深度经验相对欠缺，生态迁移需要时间",
            "零信任架构的生产级部署经验尚属浅薄",
        ],
        "matched_keywords": ["Cloud Security", "IAM", "Encryption", "Compliance", "Azure", "Security Architecture"],
    },
    {
        "title": "DevOps Engineer - Kubernetes",
        "company": "Cloudflare",
        "location": "San Francisco, CA",
        "url": "https://example.com/cloudflare-devops-1",
        "description_snippet": "Manage Kubernetes at scale. 3-5 years K8s/infrastructure. On-prem + cloud hybrid.",
        "score": 7,
        "strengths": [
            "Kubernetes 与容器编排 3+ 年实战，自定义 Operator 开发经验",
            "CI/CD 流程设计与工具链整合（GitOps、Helm、ArgoCD）能力强",
            "监控、日志、追踪（ELK/Prometheus）的全栈观测系统搭建经验丰富",
        ],
        "weaknesses": [
            "边缘计算（Edge）与 CDN 深度优化的理论理解有限，实战缺乏",
        ],
        "matched_keywords": ["Kubernetes", "Docker", "Helm", "GitOps", "Prometheus", "CI/CD", "DevOps"],
    },
    {
        "title": "Solutions Architect - AI/ML",
        "company": "AWS",
        "location": "New York, NY",
        "url": "https://example.com/aws-solutions-1",
        "description_snippet": "Design ML solutions for enterprise. 6+ years AI/ML + cloud architecture.",
        "score": 6,
        "strengths": [
            "AI/ML 完整技能栈与 AWS 生态（SageMaker、Lambda）理解深刻",
            "客户需求分析与解决方案设计能力成熟，跨职能协调经验足",
            "成本优化与合规性设计的平衡把控能力达到中等偏上",
        ],
        "weaknesses": [
            "大型企业账户管理（多账号、管理合并）的一线实操经验缺乏",
            "对垂直行业特定的监管需求（如金融、医疗）的深度认知有限",
        ],
        "matched_keywords": ["AWS", "SageMaker", "Machine Learning", "Cloud Architecture", "Python", "TensorFlow"],
    },
    {
        "title": "QA Engineer - Automation",
        "company": "Google",
        "location": "Mountain View, CA",
        "url": "https://example.com/google-qa-1",
        "description_snippet": "Test automation for core products. 3-4 years exp, C++ or Python. Strong testing mindset.",
        "score": 5,
        "strengths": [
            "自动化测试框架设计（Pytest、Robot Framework）与 CI 集成经验稳健",
            "对测试策略（单元、集成、E2E）的权衡理解清晰，性本善",
            "问题诊断与根因分析的系统化方法论有所掌握",
        ],
        "weaknesses": [
            "对性能测试（负载、压力、容量规划）的理论与工具深度不足",
            "大规模分布式系统（如搜索、地图）测试的场景经验缺少",
        ],
        "matched_keywords": ["Testing", "Python", "C++", "Pytest", "Automation", "CI/CD"],
    },
    {
        "title": "Junior Data Scientist - Analytics",
        "company": "Netflix",
        "location": "Los Gatos, CA",
        "url": "https://example.com/netflix-ds-1",
        "description_snippet": "Analyze user behavior & recommend systems. 2-3 years SQL/Python/R. Statistics knowledge required.",
        "score": 8,
        "strengths": [
            "SQL 与统计学基础扎实，数据探索与假设检验能力突出",
            "Python 数据科学栈（pandas、scikit-learn）实操经验充分",
            "A/B 测试设计与解释能力清晰，量化决策思维成熟",
        ],
        "weaknesses": [
            "对大规模推荐系统架构（协同过滤、矩阵分解、深度学习排序）的理论理解仍需深化",
        ],
        "matched_keywords": ["Python", "SQL", "Statistics", "Pandas", "Scikit-learn", "A/B Testing"],
    },
    {
        "title": "Platform Engineer - Developer Experience",
        "company": "GitLab",
        "location": "Remote",
        "url": "https://example.com/gitlab-platform-1",
        "description_snippet": "Improve developer experience. Build internal platforms. 3-5 years backend/platform exp.",
        "score": 7,
        "strengths": [
            "开发工具链优化与内部平台建设的系统化思路清晰（观测、自动化、自助）",
            "Go 与 Ruby 多语言经验，对 GitOps 与 IaC 理解深入",
            "社区驱动与开源文化的亲身参与，协作沟通能力强",
        ],
        "weaknesses": [
            "对 GitLab 特定的插件与扩展机制的细节理解尚属浅薄",
        ],
        "matched_keywords": ["Go", "Ruby", "Platform Engineering", "DevOps", "Git", "Open Source"],
    },
    {
        "title": "Research Scientist - LLMs",
        "company": "Hugging Face",
        "location": "Remote",
        "url": "https://example.com/huggingface-research-1",
        "description_snippet": "Research on language models & alignment. PhD preferred. 2+ publications required.",
        "score": 8,
        "strengths": [
            "Transformer 与 LLM 的论文研读深度与实现能力兼备，创新思维活跃",
            "开源贡献（Hugging Face Hub、Transformers 库）与社区影响力显著",
            "写作能力清晰，技术表达与演讲经验充分",
        ],
        "weaknesses": [
            "对模型推理优化（量化、蒸馏、剪枝）的生产级实践经验相对欠缺",
        ],
        "matched_keywords": ["Transformers", "LLMs", "PyTorch", "Research", "NLP", "Paper Writing"],
    },
    {
        "title": "Growth Engineer - Fintech",
        "company": "Wise",
        "location": "London, UK",
        "url": "https://example.com/wise-growth-1",
        "description_snippet": "Drive user growth & monetization. 3-4 years product/growth/data exp.",
        "score": 6,
        "strengths": [
            "用户增长与数据驱动决策的框架理解深刻（AARRR、漏斗分析、cohort 分析）",
            "SQL 数据提取与 Python 分析脚本编写能力达到工程化水准",
            "A/B 测试与多变量实验的设计与解释能力成熟",
        ],
        "weaknesses": [
            "对金融产品的合规与风险约束的深度理解有限（快速学习曲线）",
            "与市场营销部门的协作经验相对缺乏，跨职能沟通需要磨合",
        ],
        "matched_keywords": ["Data Analysis", "SQL", "Python", "Growth Metrics", "A/B Testing", "Product Analytics"],
    },
    {
        "title": "Blockchain Engineer - Smart Contracts",
        "company": "Uniswap Labs",
        "location": "Remote",
        "url": "https://example.com/uniswap-blockchain-1",
        "description_snippet": "Develop smart contracts. Solidity, Rust. 4+ years blockchain exp. Security critical.",
        "score": 4,
        "strengths": [
            "Solidity 智能合约开发与审计能力达到中等水准，gas 优化意识良好",
            "DeFi 协议架构与流动性机制的原理理解扎实",
            "数学与密码学基础知识储备足够支撑安全编程",
        ],
        "weaknesses": [
            "Rust 与 Substrate 框架的学习曲线较陡，跨链技术经验欠缺",
            "对监管政策的持续演变（SEC 执法、稳定币监管）的敏感度不足",
        ],
        "matched_keywords": ["Solidity", "Ethereum", "Smart Contracts", "DeFi", "Web3", "Cryptography"],
    },
]


def seed_database(db_path: str = "jobfinder_test_cache.db"):
    """将演示数据写入 SQLite 数据库。"""

    # 创建/连接数据库
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # 确保表存在
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS job_cache (
            dedup_key           TEXT PRIMARY KEY,
            title               TEXT NOT NULL,
            company             TEXT NOT NULL,
            location            TEXT,
            description_snippet TEXT,
            url                 TEXT,
            sources             TEXT,
            fetched_at          TEXT NOT NULL,
            expires_at          TEXT,
            is_complete         INTEGER NOT NULL DEFAULT 1,
            assessment          TEXT
        );

        CREATE TABLE IF NOT EXISTS search_sessions (
            session_key         TEXT PRIMARY KEY,
            roles               TEXT NOT NULL,
            location            TEXT NOT NULL,
            seniority           TEXT NOT NULL,
            search_language     TEXT NOT NULL,
            job_dedup_keys      TEXT NOT NULL,
            created_at          TEXT NOT NULL
        );
    """)

    # 插入演示职位
    from datetime import timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_in = now + timedelta(days=7)

    job_keys = []
    for i, job in enumerate(DEMO_JOBS, 1):
        # 生成 dedup_key（简化版：company|title）
        dedup_key = f"{job['company'].lower()}|{job['title'].lower()}"
        job_keys.append(dedup_key)

        assessment = {
            "score": job["score"],
            "strengths": job["strengths"],
            "weaknesses": job["weaknesses"],
            "matched_keywords": job["matched_keywords"],
        }

        try:
            cur.execute(
                """
                INSERT OR REPLACE INTO job_cache
                (dedup_key, title, company, location, description_snippet,
                 url, sources, fetched_at, expires_at, is_complete, assessment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dedup_key,
                    job["title"],
                    job["company"],
                    job["location"],
                    job["description_snippet"],
                    job["url"],
                    json.dumps(["demo"]),
                    now.isoformat(),
                    expires_in.isoformat(),
                    1,
                    json.dumps(assessment),
                ),
            )
            print(f"[OK] {i:2d}. {job['company']:15s} - {job['title']}")
        except Exception as e:
            print(f"[FAIL] {job['company']} - {job['title']}: {e}")

    # 创建一个演示搜索会话
    import hashlib
    session_data = {
        "roles": ["AI/ML Engineer", "Backend Engineer", "Full Stack Engineer"],
        "location": "San Francisco, CA",
        "seniority": "senior",
    }
    session_key = hashlib.md5(
        json.dumps(session_data, sort_keys=True).encode()
    ).hexdigest()

    try:
        cur.execute(
            """
            INSERT OR REPLACE INTO search_sessions
            (session_key, roles, location, seniority, search_language, job_dedup_keys, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key,
                json.dumps(session_data["roles"]),
                session_data["location"],
                session_data["seniority"],
                "en",
                json.dumps(job_keys),
                now.isoformat(),
            ),
        )
        print(f"\n[OK] Created demo search session with {len(job_keys)} jobs")
    except Exception as e:
        print(f"\n[FAIL] Failed to create session: {e}")

    con.commit()
    con.close()

    print(f"\n[DONE] Demo data written to {db_path}")
    print(f"       Location: {Path(db_path).resolve()}")


if __name__ == "__main__":
    # 使用 --mock 模式的数据库路径
    db = os.getenv("CACHE_DB_PATH", "jobfinder_test_cache.db")
    seed_database(db)
