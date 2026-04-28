"""Cloud SQL PostgreSQL database layer with pgvector semantic cache."""

import json
import logging
import os
import uuid

import asyncpg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "sketchmind")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "changeme")
DB_UNIX_SOCKET = os.getenv("DB_UNIX_SOCKET", "")

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.78"))

pool: asyncpg.Pool | None = None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create connection pool and ensure tables + pgvector extension exist."""
    global pool
    connect_kwargs: dict = dict(
        database=DB_NAME, user=DB_USER, password=DB_PASS,
        min_size=2, max_size=10,
    )
    if DB_UNIX_SOCKET:
        connect_kwargs["host"] = DB_UNIX_SOCKET
    else:
        connect_kwargs["host"] = DB_HOST
        connect_kwargs["port"] = DB_PORT

    pool = await asyncpg.create_pool(**connect_kwargs)

    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id              TEXT PRIMARY KEY,
                topic           TEXT NOT NULL,
                embedding       vector(768),
                video_url       TEXT,
                status          TEXT DEFAULT 'processing',
                error           TEXT,
                created_at      TIMESTAMPTZ DEFAULT now()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_embedding_hnsw
            ON videos USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """)

        # Parent-child multi-video columns (added via ALTER for backwards compat).
        # `stage` and `message` carry the queue-pipeline progress that the
        # WebSocket reads — replaces the old in-memory `sessions` dict.
        for col, typedef in [
            ("subtopic_title", "TEXT"),
            ("parent_id", "TEXT"),
            ("subtopic_index", "INTEGER"),
            ("stage", "TEXT"),
            ("message", "TEXT"),
        ]:
            await conn.execute(
                f"ALTER TABLE videos ADD COLUMN IF NOT EXISTS {col} {typedef};"
            )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_parent_index "
            "ON videos (parent_id, subtopic_index) WHERE parent_id IS NOT NULL;"
        )

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                google_id   TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                email       TEXT UNIQUE NOT NULL,
                avatar_url  TEXT,
                created_at  TIMESTAMPTZ DEFAULT now()
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                topic       TEXT NOT NULL,
                session_id  TEXT,
                status      TEXT DEFAULT 'processing',
                created_at  TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_search_history_user
            ON search_history (user_id, created_at DESC);
        """)

        # Learning paths: ordered list of topics, each generated as a video,
        # gated sequentially in the UI (next unlocks when previous is marked done).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning_paths (
                id            TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title         TEXT NOT NULL,
                topics        JSONB NOT NULL,
                current_index INTEGER DEFAULT 0,
                created_at    TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_learning_paths_user
            ON learning_paths (user_id, created_at DESC);
        """)

        # title_embedding for L2 fuzzy per-user dedupe + L3 cross-user syllabus cache.
        await conn.execute(
            "ALTER TABLE learning_paths ADD COLUMN IF NOT EXISTS title_embedding vector(768);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_learning_paths_user_lower_title "
            "ON learning_paths (user_id, LOWER(title));"
        )
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_learning_paths_embedding_hnsw
            ON learning_paths USING hnsw (title_embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """)

        # Exact-text fast-path index for path-topic cache lookups.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_lower_topic_completed_parents "
            "ON videos (LOWER(topic)) WHERE status = 'completed' AND parent_id IS NULL;"
        )

        # Quizzes: one quiz per parent video session, shared across users.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                session_id  TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                questions   JSONB NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT now()
            );
        """)


# ---------------------------------------------------------------------------
# Semantic cache
# ---------------------------------------------------------------------------

async def check_semantic_cache(embedding: list[float]) -> dict | None:
    """Return completed videos if a semantically similar topic exists."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, topic, video_url,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM videos
            WHERE status = 'completed'
              AND parent_id IS NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 1;
            """,
            str(embedding),
        )
        if row:
            logger.info(
                "Cache lookup: best match topic=%r similarity=%.4f threshold=%.2f hit=%s",
                row["topic"], float(row["similarity"]),
                SIMILARITY_THRESHOLD, row["similarity"] >= SIMILARITY_THRESHOLD,
            )
        else:
            logger.info("Cache lookup: no completed videos found")

        if not (row and row["similarity"] >= SIMILARITY_THRESHOLD):
            return None

        parent_id = row["id"]
        children = await conn.fetch(
            """
            SELECT subtopic_title, video_url, subtopic_index
            FROM videos
            WHERE parent_id = $1 AND status = 'completed' AND video_url IS NOT NULL
            ORDER BY subtopic_index;
            """,
            parent_id,
        )
        if children:
            return {
                "id": parent_id,
                "topic": row["topic"],
                "videos": [dict(c) for c in children],
                "similarity": float(row["similarity"]),
            }
        # Legacy single-video rows with no children
        if row["video_url"]:
            return {
                "id": parent_id,
                "topic": row["topic"],
                "videos": [{
                    "subtopic_title": row["topic"],
                    "video_url": row["video_url"],
                    "subtopic_index": 0,
                }],
                "similarity": float(row["similarity"]),
            }
    return None


async def backfill_path_embeddings(
    embed_fn,
) -> int:
    """One-time backfill: compute title_embedding for any learning_paths row
    where it's still NULL (paths created before the embedding column existed,
    or before the dedupe code shipped).

    Idempotent — only touches NULL rows, so subsequent restarts no-op once
    every path has an embedding. Required for L2 fuzzy dedupe to see legacy
    paths and collapse near-misses (typos, paraphrases) against them.

    `embed_fn` is an async callable taking a string and returning a list[float],
    passed in to keep this module free of an embeddings-module dependency.
    Returns the number of rows backfilled.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title FROM learning_paths "
            "WHERE title_embedding IS NULL;"
        )
        if not rows:
            return 0
        logger.info("Backfilling title_embedding for %d legacy path(s)", len(rows))
        for r in rows:
            try:
                emb = await embed_fn(r["title"])
                await conn.execute(
                    "UPDATE learning_paths SET title_embedding = $1::vector "
                    "WHERE id = $2;",
                    str(emb), r["id"],
                )
            except Exception as exc:
                logger.warning(
                    "Backfill failed for path %s (%r): %s", r["id"], r["title"], exc,
                )
        return len(rows)


async def find_completed_parent_by_topic(topic: str) -> dict | None:
    """Exact case-insensitive topic match against completed parent videos.

    Cheap fast-path used by path-topic kick-off so two users with the same
    sub-topic title (which is the common case after the L3 syllabus cache
    copies titles verbatim) share videos without paying for a Gemini
    `normalize_topic` call. Returns the same shape as `check_semantic_cache`
    on hit, or `None` on miss.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, topic, video_url
            FROM videos
            WHERE status = 'completed'
              AND parent_id IS NULL
              AND LOWER(topic) = LOWER($1)
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            topic,
        )
        if not row:
            return None
        parent_id = row["id"]
        children = await conn.fetch(
            """
            SELECT subtopic_title, video_url, subtopic_index
            FROM videos
            WHERE parent_id = $1 AND status = 'completed' AND video_url IS NOT NULL
            ORDER BY subtopic_index;
            """,
            parent_id,
        )
        if children:
            logger.info("Path topic exact-text cache hit: %r → %s", topic, parent_id)
            return {
                "id": parent_id,
                "topic": row["topic"],
                "videos": [dict(c) for c in children],
            }
        if row["video_url"]:
            logger.info("Path topic exact-text cache hit (legacy): %r → %s", topic, parent_id)
            return {
                "id": parent_id,
                "topic": row["topic"],
                "videos": [{
                    "subtopic_title": row["topic"],
                    "video_url": row["video_url"],
                    "subtopic_index": 0,
                }],
            }
    return None


# ---------------------------------------------------------------------------
# Video session management
# ---------------------------------------------------------------------------

async def create_session(topic: str, embedding: list[float]) -> str:
    """Insert a new parent video record and return its id."""
    video_id = uuid.uuid4().hex[:12]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO videos (id, topic, embedding, status, stage, message)
            VALUES ($1, $2, $3::vector, 'processing', 'starting', 'Starting...');
            """,
            video_id, topic, str(embedding),
        )
    return video_id


async def create_subtopic_record(
    parent_id: str, subtopic_title: str, subtopic_index: int,
) -> str:
    """Insert a child video record for a subtopic. Returns its id."""
    child_id = uuid.uuid4().hex[:12]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO videos
              (id, topic, status, stage, message,
               parent_id, subtopic_title, subtopic_index)
            VALUES ($1, $2, 'processing', 'pending', 'Waiting...',
                    $3, $4, $5);
            """,
            child_id, subtopic_title, parent_id, subtopic_title, subtopic_index,
        )
    return child_id


async def update_subtopic_record(subtopic_id: str, video_url: str) -> None:
    """Mark a subtopic video as completed with its URL."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET video_url = $1, status = 'completed' WHERE id = $2;",
            video_url, subtopic_id,
        )


async def mark_subtopic_failed(subtopic_id: str, error: str) -> None:
    """Mark a subtopic video as failed."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET status = 'failed', error = $1 WHERE id = $2;",
            error[:2000], subtopic_id,
        )


async def complete_parent_session(parent_id: str) -> None:
    """Set parent status based on children: completed if any succeeded, else failed."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'completed') AS ok,
                COUNT(*) FILTER (WHERE status = 'failed') AS fail
            FROM videos WHERE parent_id = $1;
            """,
            parent_id,
        )
        new_status = "completed" if row["ok"] > 0 else "failed"
        await conn.execute(
            "UPDATE videos SET status = $1 WHERE id = $2;",
            new_status, parent_id,
        )


async def mark_failed(video_id: str, error: str) -> None:
    """Mark a video as failed with the error message."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET status = 'failed', error = $1, stage = 'failed' WHERE id = $2;",
            error[:2000], video_id,
        )


async def update_session_stage(
    session_id: str, stage: str, message: str | None = None,
) -> None:
    """Update the parent session's stage (starting/researching/generating/completed/failed)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET stage = $1, message = COALESCE($2, message) "
            "WHERE id = $3 AND parent_id IS NULL;",
            stage, message, session_id,
        )


async def update_subtopic_stage(
    parent_id: str, index: int,
    stage: str, message: str | None = None,
    video_url: str | None = None, error: str | None = None,
) -> None:
    """Update a child subtopic row by (parent_id, index). Sets terminal `status`
    + `video_url` / `error` columns when stage is 'completed' or 'failed' so the
    existing semantic-cache and history queries keep working."""
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


async def get_session_status(session_id: str) -> dict | None:
    """Return the WebSocket-shaped status payload for a session: parent stage +
    full ordered subtopic list. Replaces the in-memory `sessions` dict."""
    async with pool.acquire() as conn:
        parent = await conn.fetchrow(
            "SELECT id, topic, status, stage, message, error "
            "FROM videos WHERE id = $1 AND parent_id IS NULL;",
            session_id,
        )
        if not parent:
            return None
        children = await conn.fetch(
            "SELECT subtopic_index, subtopic_title, stage, message, "
            "       video_url, status, error "
            "FROM videos WHERE parent_id = $1 ORDER BY subtopic_index;",
            session_id,
        )

        if parent["status"] in ("completed", "failed"):
            stage = parent["status"]
        else:
            stage = parent["stage"] or "starting"

        return {
            "stage": stage,
            "topic": parent["topic"],
            "error": parent["error"],
            "subtopics": [
                {
                    "index": c["subtopic_index"],
                    "subtopic_title": c["subtopic_title"],
                    "stage": c["stage"] or "pending",
                    "message": c["message"] or "Waiting...",
                    "video_url": c["video_url"],
                    "error": c["error"],
                }
                for c in children
            ],
        }


async def find_active_session_by_topic(topic: str) -> str | None:
    """Look up any in-flight parent session whose topic matches (case-insensitive).
    Used to dedupe duplicate /api/generate calls for the same topic across users."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM videos "
            "WHERE parent_id IS NULL "
            "  AND status = 'processing' "
            "  AND LOWER(topic) = LOWER($1) "
            "ORDER BY created_at DESC LIMIT 1;",
            topic.strip(),
        )
        return row["id"] if row else None


async def get_all_videos() -> list[dict]:
    """Return all completed parent videos with their subtopic children, newest first."""
    async with pool.acquire() as conn:
        parents = await conn.fetch(
            """
            SELECT id, topic, video_url, status, created_at
            FROM videos
            WHERE parent_id IS NULL AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 50;
            """,
        )
        results = []
        for p in parents:
            children = await conn.fetch(
                """
                SELECT subtopic_title, video_url, subtopic_index
                FROM videos
                WHERE parent_id = $1 AND status = 'completed' AND video_url IS NOT NULL
                ORDER BY subtopic_index;
                """,
                p["id"],
            )
            if children:
                results.append({
                    "id": p["id"],
                    "topic": p["topic"],
                    "created_at": p["created_at"],
                    "videos": [dict(c) for c in children],
                })
            elif p.get("video_url"):
                results.append({
                    "id": p["id"],
                    "topic": p["topic"],
                    "created_at": p["created_at"],
                    "videos": [{
                        "subtopic_title": p["topic"],
                        "video_url": p["video_url"],
                        "subtopic_index": 0,
                    }],
                })
        return results


# ---------------------------------------------------------------------------
# User & Search History
# ---------------------------------------------------------------------------

async def upsert_google_user(
    user_id: str, google_id: str, name: str, email: str, avatar_url: str,
) -> dict:
    """Insert or update a user from Google OAuth. Returns the user dict."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE google_id = $1;", google_id,
        )
        if row:
            await conn.execute(
                "UPDATE users SET name = $1, avatar_url = $2 WHERE google_id = $3;",
                name, avatar_url, google_id,
            )
            return {**dict(row), "name": name, "avatar_url": avatar_url}

        await conn.execute(
            """INSERT INTO users (id, google_id, name, email, avatar_url)
               VALUES ($1, $2, $3, $4, $5);""",
            user_id, google_id, name, email, avatar_url,
        )
        return {
            "id": user_id, "google_id": google_id,
            "name": name, "email": email, "avatar_url": avatar_url,
        }


async def add_search_history(
    history_id: str, user_id: str, topic: str,
    session_id: str | None = None, status: str = "completed",
) -> None:
    """Record a search in user history. Skips duplicates for the same session."""
    async with pool.acquire() as conn:
        if session_id:
            exists = await conn.fetchval(
                """SELECT 1 FROM search_history
                   WHERE user_id = $1 AND session_id = $2 AND status = 'completed'
                   LIMIT 1;""",
                user_id, session_id,
            )
            if exists:
                return
        await conn.execute(
            """INSERT INTO search_history (id, user_id, topic, session_id, status)
               VALUES ($1, $2, $3, $4, $5);""",
            history_id, user_id, topic, session_id, status,
        )


async def reconcile_stale_history() -> None:
    """On startup, fix search_history rows stuck as 'processing'.

    If the linked video session finished (completed/failed), sync the status.
    If the session is still 'processing' (pipeline was interrupted), mark as failed.
    """
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE search_history sh
            SET status = v.status
            FROM videos v
            WHERE sh.session_id = v.id
              AND v.parent_id IS NULL
              AND sh.status = 'processing'
              AND v.status IN ('completed', 'failed');
        """)
        await conn.execute("""
            UPDATE search_history
            SET status = 'failed'
            WHERE status = 'processing'
              AND created_at < now() - interval '10 minutes';
        """)


async def get_user_history(user_id: str, limit: int = 50) -> list[dict]:
    """Return completed history entries for a user with attached video data."""
    async with pool.acquire() as conn:
        # Reconcile any stale processing entries for this user
        await conn.execute("""
            UPDATE search_history sh
            SET status = v.status
            FROM videos v
            WHERE sh.session_id = v.id
              AND v.parent_id IS NULL
              AND sh.user_id = $1
              AND sh.status = 'processing'
              AND v.status IN ('completed', 'failed');
        """, user_id)

        rows = await conn.fetch(
            """SELECT sh.id, sh.topic, sh.session_id, sh.status, sh.created_at,
                      v.id AS video_id
               FROM search_history sh
               LEFT JOIN videos v ON v.id = sh.session_id AND v.parent_id IS NULL
               WHERE sh.user_id = $1
                 AND sh.status = 'completed'
               ORDER BY sh.created_at DESC
               LIMIT $2;""",
            user_id, limit,
        )

        results = []
        for r in rows:
            entry = dict(r)
            if r["session_id"]:
                children = await conn.fetch(
                    """SELECT subtopic_title, video_url, subtopic_index
                       FROM videos
                       WHERE parent_id = $1 AND status = 'completed' AND video_url IS NOT NULL
                       ORDER BY subtopic_index;""",
                    r["session_id"],
                )
                entry["videos"] = [dict(c) for c in children]
            else:
                entry["videos"] = []
            results.append(entry)
        return results


async def delete_search_history(history_id: str, user_id: str) -> bool:
    """Delete a history entry. Returns True if deleted."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM search_history WHERE id = $1 AND user_id = $2;",
            history_id, user_id,
        )
        return result == "DELETE 1"


async def clear_user_history(user_id: str) -> None:
    """Delete all history entries for a user."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM search_history WHERE user_id = $1;", user_id,
        )


# ---------------------------------------------------------------------------
# Learning paths
# ---------------------------------------------------------------------------

async def create_learning_path(
    path_id: str, user_id: str, title: str, topics: list[str],
    embedding: list[float] | None = None,
) -> dict:
    """Create a new learning path with the given ordered topics."""
    topics_data = [
        {"topic": t, "session_id": None, "completed": False}
        for t in topics
    ]
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO learning_paths
               (id, user_id, title, topics, current_index, title_embedding)
               VALUES ($1, $2, $3, $4::jsonb, 0, $5::vector);""",
            path_id, user_id, title, json.dumps(topics_data),
            str(embedding) if embedding is not None else None,
        )
    return {
        "id": path_id, "title": title, "topics": topics_data, "current_index": 0,
    }


async def find_user_path_by_exact_title(
    user_id: str, title: str,
) -> str | None:
    """L0: case-insensitive exact-title dedupe. Zero LLM cost."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM learning_paths "
            "WHERE user_id = $1 AND LOWER(title) = LOWER($2) "
            "ORDER BY created_at DESC LIMIT 1;",
            user_id, title,
        )
        return row["id"] if row else None


async def find_user_path_by_embedding(
    user_id: str, embedding: list[float],
) -> str | None:
    """L2: fuzzy per-user dedupe via pgvector cosine."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, title,
                   1 - (title_embedding <=> $2::vector) AS similarity
            FROM learning_paths
            WHERE user_id = $1 AND title_embedding IS NOT NULL
            ORDER BY title_embedding <=> $2::vector
            LIMIT 1;
            """,
            user_id, str(embedding),
        )
        if not row:
            return None
        logger.info(
            "Path dedupe (L2 fuzzy): user=%s best=%r sim=%.4f hit=%s",
            user_id, row["title"], float(row["similarity"]),
            row["similarity"] >= SIMILARITY_THRESHOLD,
        )
        return row["id"] if row["similarity"] >= SIMILARITY_THRESHOLD else None


async def check_path_outline_cache(embedding: list[float]) -> list[str] | None:
    """L3: cross-user syllabus cache. Returns the topic-titles list of the
    most-similar existing path (any user), or None if below threshold."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT topics, title,
                   1 - (title_embedding <=> $1::vector) AS similarity
            FROM learning_paths
            WHERE title_embedding IS NOT NULL
            ORDER BY title_embedding <=> $1::vector
            LIMIT 1;
            """,
            str(embedding),
        )
        if not row:
            return None
        logger.info(
            "Path syllabus cache (L3): best=%r sim=%.4f hit=%s",
            row["title"], float(row["similarity"]),
            row["similarity"] >= SIMILARITY_THRESHOLD,
        )
        if row["similarity"] < SIMILARITY_THRESHOLD:
            return None
        topics_data = (
            json.loads(row["topics"]) if isinstance(row["topics"], str) else row["topics"]
        )
        return [t["topic"] for t in topics_data if t.get("topic")]


async def get_user_paths(user_id: str) -> list[dict]:
    """Return all learning paths for the user, newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, title, topics, current_index, created_at
               FROM learning_paths
               WHERE user_id = $1
               ORDER BY created_at DESC;""",
            user_id,
        )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "topics": json.loads(r["topics"]) if isinstance(r["topics"], str) else r["topics"],
                "current_index": r["current_index"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]


async def get_learning_path(path_id: str, user_id: str) -> dict | None:
    """Return a single learning path with attached video data per topic."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, title, topics, current_index, created_at
               FROM learning_paths
               WHERE id = $1 AND user_id = $2;""",
            path_id, user_id,
        )
        if not row:
            return None

        topics = json.loads(row["topics"]) if isinstance(row["topics"], str) else row["topics"]

        # Attach video data for any topic that has a session_id.
        for t in topics:
            sid = t.get("session_id")
            if not sid:
                t["videos"] = []
                continue
            children = await conn.fetch(
                """SELECT subtopic_title, video_url, subtopic_index
                   FROM videos
                   WHERE parent_id = $1 AND status = 'completed' AND video_url IS NOT NULL
                   ORDER BY subtopic_index;""",
                sid,
            )
            t["videos"] = [dict(c) for c in children]

        return {
            "id": row["id"],
            "title": row["title"],
            "topics": topics,
            "current_index": row["current_index"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }


async def attach_session_to_path_topic(
    path_id: str, user_id: str, index: int, session_id: str,
) -> bool:
    """Set the session_id for a topic at a given index. Returns True on success."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE learning_paths
               SET topics = jsonb_set(topics, ARRAY[$3::text, 'session_id'], to_jsonb($4::text))
               WHERE id = $1 AND user_id = $2;""",
            path_id, user_id, str(index), session_id,
        )
        return result == "UPDATE 1"


async def mark_path_topic_completed(
    path_id: str, user_id: str, index: int,
) -> dict | None:
    """Mark a topic as completed and advance current_index past consecutive done topics."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT topics, current_index FROM learning_paths WHERE id = $1 AND user_id = $2;",
            path_id, user_id,
        )
        if not row:
            return None
        topics = json.loads(row["topics"]) if isinstance(row["topics"], str) else row["topics"]
        if index < 0 or index >= len(topics):
            return None
        topics[index]["completed"] = True

        # Advance current_index past any consecutive completed topics from the start.
        new_current = row["current_index"]
        while new_current < len(topics) and topics[new_current].get("completed"):
            new_current += 1

        await conn.execute(
            """UPDATE learning_paths
               SET topics = $3::jsonb, current_index = $4
               WHERE id = $1 AND user_id = $2;""",
            path_id, user_id, json.dumps(topics), new_current,
        )
        return {"current_index": new_current, "topics": topics}


async def delete_learning_path(path_id: str, user_id: str) -> bool:
    """Delete a learning path. Returns True if deleted."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM learning_paths WHERE id = $1 AND user_id = $2;",
            path_id, user_id,
        )
        return result == "DELETE 1"


async def append_path_topic(path_id: str, user_id: str, topic: str) -> dict | None:
    """Append a new topic to the end of a path. Returns updated topics list."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT topics FROM learning_paths WHERE id = $1 AND user_id = $2;",
            path_id, user_id,
        )
        if not row:
            return None
        topics = json.loads(row["topics"]) if isinstance(row["topics"], str) else row["topics"]
        topics.append({"topic": topic, "session_id": None, "completed": False})
        await conn.execute(
            """UPDATE learning_paths SET topics = $3::jsonb
               WHERE id = $1 AND user_id = $2;""",
            path_id, user_id, json.dumps(topics),
        )
        return {"topics": topics}


# ---------------------------------------------------------------------------
# Quizzes
# ---------------------------------------------------------------------------

async def get_session_topic_and_subtopics(session_id: str) -> dict | None:
    """Return {topic, subtopics: [str]} for a video session — used as
    quiz-generation context. None if session not found."""
    async with pool.acquire() as conn:
        parent = await conn.fetchrow(
            "SELECT topic FROM videos WHERE id = $1 AND parent_id IS NULL;",
            session_id,
        )
        if not parent:
            return None
        children = await conn.fetch(
            "SELECT subtopic_title FROM videos "
            "WHERE parent_id = $1 AND status = 'completed' "
            "ORDER BY subtopic_index;",
            session_id,
        )
        return {
            "topic": parent["topic"],
            "subtopics": [c["subtopic_title"] for c in children if c["subtopic_title"]],
        }


async def get_quiz(session_id: str) -> list[dict] | None:
    """Return the cached quiz (full questions including answers) or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT questions FROM quizzes WHERE session_id = $1;",
            session_id,
        )
        if not row:
            return None
        return (
            json.loads(row["questions"]) if isinstance(row["questions"], str) else row["questions"]
        )


async def save_quiz(session_id: str, questions: list[dict]) -> None:
    """Persist a generated quiz. Idempotent via ON CONFLICT."""
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO quizzes (session_id, questions)
               VALUES ($1, $2::jsonb)
               ON CONFLICT (session_id) DO UPDATE
                 SET questions = EXCLUDED.questions;""",
            session_id, json.dumps(questions),
        )
