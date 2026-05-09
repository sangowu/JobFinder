from __future__ import annotations

from datetime import datetime
from typing import Callable

from jobradar.schemas import CVOptimization, CoverLetter, InterviewPrep


def get_interview_prep(
    conn_factory: Callable,
    description_hash_fn: Callable[[str], str],
    job_id: str,
    cv_hash: str,
    description: str = "",
) -> InterviewPrep | None:
    with conn_factory() as con:
        row = con.execute(
            "SELECT prep_json, description_hash FROM interview_preps WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != description_hash_fn(description):
        return None
    return InterviewPrep.model_validate_json(row["prep_json"])


def save_interview_prep(
    conn_factory: Callable,
    description_hash_fn: Callable[[str], str],
    prep: InterviewPrep,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with conn_factory() as con:
        con.execute(
            """
            INSERT INTO interview_preps
              (job_id, cv_hash, description_hash, prep_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              prep_json = excluded.prep_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                prep.job_id,
                prep.cv_hash,
                description_hash_fn(description),
                prep.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_cover_letter(
    conn_factory: Callable,
    description_hash_fn: Callable[[str], str],
    job_id: str,
    cv_hash: str,
    description: str = "",
) -> CoverLetter | None:
    with conn_factory() as con:
        row = con.execute(
            "SELECT letter_json, description_hash FROM cover_letters WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != description_hash_fn(description):
        return None
    return CoverLetter.model_validate_json(row["letter_json"])


def save_cover_letter(
    conn_factory: Callable,
    description_hash_fn: Callable[[str], str],
    letter: CoverLetter,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with conn_factory() as con:
        con.execute(
            """
            INSERT INTO cover_letters
              (job_id, cv_hash, description_hash, letter_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              letter_json = excluded.letter_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                letter.job_id,
                letter.cv_hash,
                description_hash_fn(description),
                letter.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_cv_optimization(
    conn_factory: Callable,
    description_hash_fn: Callable[[str], str],
    job_id: str,
    cv_hash: str,
    description: str = "",
) -> CVOptimization | None:
    with conn_factory() as con:
        row = con.execute(
            "SELECT optimization_json, description_hash FROM cv_optimizations WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
    if row is None:
        return None
    if description and row["description_hash"] != description_hash_fn(description):
        return None
    return CVOptimization.model_validate_json(row["optimization_json"])


def save_cv_optimization(
    conn_factory: Callable,
    description_hash_fn: Callable[[str], str],
    optimization: CVOptimization,
    description: str,
    model_name: str = "",
    prompt_version: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with conn_factory() as con:
        con.execute(
            """
            INSERT INTO cv_optimizations
              (job_id, cv_hash, description_hash, optimization_json, model_name, prompt_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cv_hash) DO UPDATE SET
              description_hash = excluded.description_hash,
              optimization_json = excluded.optimization_json,
              model_name = excluded.model_name,
              prompt_version = excluded.prompt_version,
              updated_at = excluded.updated_at
            """,
            (
                optimization.job_id,
                optimization.cv_hash,
                description_hash_fn(description),
                optimization.model_dump_json(),
                model_name,
                prompt_version,
                now,
                now,
            ),
        )


def get_job_artifacts(
    conn_factory: Callable,
    description_hash_fn: Callable[[str], str],
    job_id: str,
    cv_hash: str,
    description: str = "",
) -> dict[str, dict]:
    description_hash = description_hash_fn(description) if description else ""
    with conn_factory() as con:
        prep_row = con.execute(
            "SELECT prep_json, description_hash, updated_at FROM interview_preps WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
        letter_row = con.execute(
            "SELECT letter_json, description_hash, updated_at FROM cover_letters WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()
        optimization_row = con.execute(
            "SELECT optimization_json, description_hash, updated_at FROM cv_optimizations WHERE job_id = ? AND cv_hash = ?",
            (job_id, cv_hash),
        ).fetchone()

    def _entry(row, field: str, model_cls):
        if row is None:
            return {"exists": False, "stale": False, "updated_at": None, "data": None}
        stale = bool(description_hash) and row["description_hash"] != description_hash
        return {
            "exists": not stale,
            "stale": stale,
            "updated_at": row["updated_at"],
            "data": None if stale else model_cls.model_validate_json(row[field]).model_dump(mode="json"),
        }

    return {
        "interview_prep": _entry(prep_row, "prep_json", InterviewPrep),
        "cover_letter": _entry(letter_row, "letter_json", CoverLetter),
        "cv_optimization": _entry(optimization_row, "optimization_json", CVOptimization),
    }
