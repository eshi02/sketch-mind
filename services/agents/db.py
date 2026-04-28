"""Lightweight DB writer for the agents service.

Mirrors the connection pattern used by services/api/database.py so the same
Cloud SQL Unix socket / TCP host wiring works in production and local dev.
Agents writes per-stage progress to the `videos` table (the source of truth
the API's WebSocket reads from) and finalizes the parent + search history
when the last sibling subtopic settles.
"""
from __future__ import annotations

import logging
import os
import uuid

import asyncpg

logger = logging.getLogger(__name__)

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "sketchmind")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "changeme")
DB_UNIX_SOCKET = os.getenv("DB_UNIX_SOCKET", "")

pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """Create connection pool. Schema is owned by the API service — agents
    only reads/writes to existing tables."""
    global pool
    connect_kwargs: dict = dict(
        database=DB_NAME, user=DB_USER, password=DB_PASS,
        min_size=1, max_size=4,
    )
    if DB_UNIX_SOCKET:
        connect_kwargs["host"] = DB_UNIX_SOCKET
    else:
        connect_kwargs["host"] = DB_HOST
        connect_kwargs["port"] = DB_PORT
    pool = await asyncpg.create_pool(**connect_kwargs)
    logger.info("agents DB pool ready")


async def close_db() -> None:
    if pool is not None:
        await pool.close()


async def update_subtopic_stage(
    parent_id: str, index: int,
    stage: str, message: str | None = None,
    video_url: str | None = None, error: str | None = None,
) -> None:
    """Write a stage transition for one subtopic. Sets `status` + `video_url` /
    `error` columns when stage is terminal so the existing semantic-cache and
    history queries keep working unchanged."""
    fields = ["stage = $3", "message = COALESCE($4, message)"]
    params: list = [parent_id, index, stage, message]
    if stage == "completed" and video_url:
        fields += ["status = 'completed'", f"video_url = ${len(params)+1}"]
        params.append(video_url)
    elif stage == "failed":
        fields += ["status = 'failed'"]
        if error:
            fields.append(f"error = ${len(params)+1}")
            params.append(error[:2000])
    sql = (
        "UPDATE videos SET " + ", ".join(fields) +
        " WHERE parent_id = $1 AND subtopic_index = $2;"
    )
    async with pool.acquire() as conn:
        await conn.execute(sql, *params)


async def finalize_if_all_done(
    parent_id: str, user_id: str | None, topic: str,
) -> bool:
    """Atomically check whether every sibling subtopic for `parent_id` has
    settled (status in completed/failed); if so, mark the parent and write
    search history. No-ops if any subtopic is still processing.

    Race-safe: uses a row lock on the parent so two finishing siblings can't
    both try to finalize simultaneously. Returns True iff this call performed
    the finalization.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            parent = await conn.fetchrow(
                "SELECT status FROM videos WHERE id = $1 AND parent_id IS NULL "
                "FOR UPDATE;",
                parent_id,
            )
            if parent is None or parent["status"] in ("completed", "failed"):
                return False

            counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'completed') AS ok,
                    COUNT(*) FILTER (WHERE status = 'failed') AS fail,
                    COUNT(*) FILTER (WHERE status = 'processing') AS pending
                FROM videos WHERE parent_id = $1;
                """,
                parent_id,
            )
            if counts["pending"] > 0 or counts["total"] == 0:
                return False

            new_status = "completed" if counts["ok"] > 0 else "failed"
            new_stage = new_status
            new_message = (
                "All videos ready" if new_status == "completed"
                else "All subtopic videos failed"
            )
            new_error = None if new_status == "completed" else "All subtopic videos failed"

            await conn.execute(
                "UPDATE videos SET status = $1, stage = $2, message = $3, error = $4 "
                "WHERE id = $5;",
                new_status, new_stage, new_message, new_error, parent_id,
            )

            if user_id and new_status == "completed":
                exists = await conn.fetchval(
                    "SELECT 1 FROM search_history "
                    "WHERE user_id = $1 AND session_id = $2 AND status = 'completed' "
                    "LIMIT 1;",
                    user_id, parent_id,
                )
                if not exists:
                    await conn.execute(
                        "INSERT INTO search_history (id, user_id, topic, session_id, status) "
                        "VALUES ($1, $2, $3, $4, 'completed');",
                        uuid.uuid4().hex[:12], user_id, topic, parent_id,
                    )

            return True
