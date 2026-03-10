"""Neon serverless PostgreSQL connection and CRUD operations.

Connection (in order of precedence):
  1. NEON_DATABASE_URL env var — set this for local development.
  2. Databricks Secret scope `study-buddy`, key `neon_database_url` — read via
     the app's service principal at runtime inside a Databricks App.

Setup (one-time):
  databricks secrets create-scope study-buddy
  databricks secrets put-secret study-buddy neon_database_url \\
      --string-value "postgresql://user:pass@host/dbname?sslmode=require"
"""

import base64
import os
from typing import Optional

from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine, text


def _get_neon_url() -> str:
    """Return the Neon connection URL from env var or Databricks Secrets."""
    url = os.getenv("NEON_DATABASE_URL")
    if url:
        return url

    # In the Databricks App runtime the service principal credentials are
    # auto-injected, so WorkspaceClient() works without any configuration.
    w = WorkspaceClient()
    resp = w.secrets.get_secret(scope="study-buddy", key="neon_database_url")
    # The Databricks Secrets API returns the value base64-encoded
    return base64.b64decode(resp.value).decode("utf-8")


def get_engine():
    """Create a SQLAlchemy engine using the Neon connection URL."""
    url = _get_neon_url()

    # Ensure the psycopg3 async-capable driver prefix is used with SQLAlchemy
    url = url.replace("postgres://", "postgresql://", 1)
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        connect_args={"sslmode": "require"},
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS study_buddy;

CREATE TABLE IF NOT EXISTS study_buddy.users (
    user_id      SERIAL PRIMARY KEY,
    username     VARCHAR(200) UNIQUE NOT NULL,
    display_name VARCHAR(200),
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS study_buddy.exam_sessions (
    session_id      SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES study_buddy.users(user_id),
    exam_name       VARCHAR(200) NOT NULL,
    exam_file       VARCHAR(200) NOT NULL,
    total_questions INTEGER NOT NULL,
    started_at      TIMESTAMP DEFAULT NOW(),
    completed_at    TIMESTAMP,
    status          VARCHAR(20) DEFAULT 'in_progress'
);

CREATE TABLE IF NOT EXISTS study_buddy.session_answers (
    answer_id       SERIAL PRIMARY KEY,
    session_id      INTEGER REFERENCES study_buddy.exam_sessions(session_id),
    question_id     INTEGER NOT NULL,
    section_id      INTEGER NOT NULL,
    section_title   VARCHAR(200) NOT NULL,
    selected_answer VARCHAR(1),
    correct_answer  VARCHAR(1) NOT NULL,
    is_correct      BOOLEAN NOT NULL,
    answered_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE (session_id, question_id)
)
"""


def init_schema(engine) -> None:
    """Idempotently create the study_buddy schema and tables."""
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def upsert_user(engine, username: str, display_name: str) -> int:
    """Insert or update a user record and return the user_id."""
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO study_buddy.users (username, display_name)
                VALUES (:username, :display_name)
                ON CONFLICT (username) DO UPDATE
                    SET display_name = EXCLUDED.display_name
                RETURNING user_id
            """),
            {"username": username, "display_name": display_name},
        )
        return result.scalar()


def create_session(
    engine, user_id: int, exam_name: str, exam_file: str, total_q: int
) -> int:
    """Create a new exam session and return the session_id."""
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO study_buddy.exam_sessions
                    (user_id, exam_name, exam_file, total_questions)
                VALUES (:user_id, :exam_name, :exam_file, :total_questions)
                RETURNING session_id
            """),
            {
                "user_id": user_id,
                "exam_name": exam_name,
                "exam_file": exam_file,
                "total_questions": total_q,
            },
        )
        return result.scalar()


def save_answer(
    engine, session_id: int, question: dict, selected: str, is_correct: bool
) -> None:
    """Persist an answer immediately. Upserts on (session_id, question_id)."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO study_buddy.session_answers
                    (session_id, question_id, section_id, section_title,
                     selected_answer, correct_answer, is_correct)
                VALUES
                    (:session_id, :question_id, :section_id, :section_title,
                     :selected_answer, :correct_answer, :is_correct)
                ON CONFLICT (session_id, question_id) DO UPDATE SET
                    selected_answer = EXCLUDED.selected_answer,
                    is_correct      = EXCLUDED.is_correct,
                    answered_at     = NOW()
            """),
            {
                "session_id": session_id,
                "question_id": question["id"],
                "section_id": question["section_id"],
                "section_title": question["section_title"],
                "selected_answer": selected,
                "correct_answer": question["correct_answer"],
                "is_correct": is_correct,
            },
        )


def finalize_session(engine, session_id: int, status: str) -> None:
    """Set completed_at and status on a session (status: completed | quit)."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE study_buddy.exam_sessions
                SET status = :status, completed_at = NOW()
                WHERE session_id = :session_id
            """),
            {"session_id": session_id, "status": status},
        )


def get_session_info(engine, session_id: int) -> Optional[dict]:
    """Return session metadata for a given session_id."""
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT session_id, exam_name, total_questions,
                       status, started_at, completed_at
                FROM study_buddy.exam_sessions
                WHERE session_id = :session_id
            """),
            {"session_id": session_id},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None


def get_section_scores(engine, session_id: int) -> list[dict]:
    """Return per-section score breakdown for a session."""
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT
                    section_id,
                    section_title,
                    COUNT(*)                                               AS total_questions,
                    SUM(CASE WHEN is_correct THEN 1 ELSE 0 END)           AS correct_answers,
                    ROUND(
                        100.0 * SUM(CASE WHEN is_correct THEN 1 ELSE 0 END)
                        / COUNT(*), 1
                    )                                                      AS score_pct
                FROM study_buddy.session_answers
                WHERE session_id = :session_id
                GROUP BY section_id, section_title
                ORDER BY section_id
            """),
            {"session_id": session_id},
        )
        return [dict(row._mapping) for row in result]


def get_user_history(engine, user_id: int) -> list[dict]:
    """Return all sessions for a user, newest first, with aggregate scores."""
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT
                    es.session_id,
                    es.exam_name,
                    es.exam_file,
                    es.total_questions,
                    es.started_at,
                    es.completed_at,
                    es.status,
                    COUNT(sa.answer_id)                                       AS answered_count,
                    SUM(CASE WHEN sa.is_correct THEN 1 ELSE 0 END)           AS correct_count,
                    ROUND(
                        100.0 * SUM(CASE WHEN sa.is_correct THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(sa.answer_id), 0), 1
                    )                                                          AS score_pct
                FROM study_buddy.exam_sessions es
                LEFT JOIN study_buddy.session_answers sa
                    ON es.session_id = sa.session_id
                WHERE es.user_id = :user_id
                GROUP BY es.session_id, es.exam_name, es.exam_file,
                         es.total_questions, es.started_at,
                         es.completed_at, es.status
                ORDER BY es.started_at DESC
            """),
            {"user_id": user_id},
        )
        return [dict(row._mapping) for row in result]
