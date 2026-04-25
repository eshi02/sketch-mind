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

        # Parent-child multi-video columns (added via ALTER for backwards compat)
        for col, typedef in [
            ("subtopic_title", "TEXT"),
            ("parent_id", "TEXT"),
            ("subtopic_index", "INTEGER"),
        ]:
            await conn.execute(
                f"ALTER TABLE videos ADD COLUMN IF NOT EXISTS {col} {typedef};"
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


# ---------------------------------------------------------------------------
# Video session management
# ---------------------------------------------------------------------------

async def create_session(topic: str, embedding: list[float]) -> str:
    """Insert a new parent video record and return its id."""
    video_id = uuid.uuid4().hex[:12]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO videos (id, topic, embedding, status)
            VALUES ($1, $2, $3::vector, 'processing');
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
            INSERT INTO videos (id, topic, status, parent_id, subtopic_title, subtopic_index)
            VALUES ($1, $2, 'processing', $3, $4, $5);
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
            "UPDATE videos SET status = 'failed', error = $1 WHERE id = $2;",
            error[:2000], video_id,
        )


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
) -> dict:
    """Create a new learning path with the given ordered topics."""
    topics_data = [
        {"topic": t, "session_id": None, "completed": False}
        for t in topics
    ]
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO learning_paths (id, user_id, title, topics, current_index)
               VALUES ($1, $2, $3, $4::jsonb, 0);""",
            path_id, user_id, title, json.dumps(topics_data),
        )
    return {
        "id": path_id, "title": title, "topics": topics_data, "current_index": 0,
    }


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
