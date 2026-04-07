"""Cloud SQL PostgreSQL database layer with pgvector semantic cache."""
import os, uuid, logging
import asyncpg

logger = logging.getLogger(__name__)

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "sketchmind")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "changeme")
DB_UNIX_SOCKET = os.getenv("DB_UNIX_SOCKET", "")  # e.g. /cloudsql/PROJECT:REGION:INSTANCE

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.78"))

pool: asyncpg.Pool | None = None


async def init_db():
    """Create connection pool and ensure tables + pgvector extension exist."""
    global pool
    connect_kwargs = dict(
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
                id          TEXT PRIMARY KEY,
                topic       TEXT NOT NULL,
                embedding   vector(768),
                video_url   TEXT,
                status      TEXT DEFAULT 'processing',
                error       TEXT,
                created_at  TIMESTAMPTZ DEFAULT now()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_embedding_hnsw
            ON videos USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """)
        # New columns for parent-child multi-video support
        for col, typedef in [
            ("subtopic_title", "TEXT"),
            ("parent_id", "TEXT"),
            ("subtopic_index", "INTEGER"),
        ]:
            await conn.execute(f"""
                ALTER TABLE videos ADD COLUMN IF NOT EXISTS {col} {typedef};
            """)


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
        if row and row["similarity"] >= SIMILARITY_THRESHOLD:
            parent_id = row["id"]
            # Fetch child subtopic videos
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
            # Backward compat: old single-video rows with no children
            if row["video_url"]:
                return {
                    "id": parent_id,
                    "topic": row["topic"],
                    "videos": [{"subtopic_title": row["topic"],
                                "video_url": row["video_url"],
                                "subtopic_index": 0}],
                    "similarity": float(row["similarity"]),
                }
    return None


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
    parent_id: str, subtopic_title: str, subtopic_index: int
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


async def update_subtopic_record(subtopic_id: str, video_url: str):
    """Mark a subtopic video as completed with its URL."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET video_url = $1, status = 'completed' WHERE id = $2;",
            video_url, subtopic_id,
        )


async def mark_subtopic_failed(subtopic_id: str, error: str):
    """Mark a subtopic video as failed."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET status = 'failed', error = $1 WHERE id = $2;",
            error[:2000], subtopic_id,
        )


async def complete_parent_session(parent_id: str):
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


async def update_video_record(video_id: str, video_url: str):
    """Mark a video as completed with its URL (legacy single-video compat)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET video_url = $1, status = 'completed' WHERE id = $2;",
            video_url, video_id,
        )


async def mark_failed(video_id: str, error: str):
    """Mark a video as failed with the error message."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE videos SET status = 'failed', error = $1 WHERE id = $2;",
            error[:2000], video_id,
        )


async def get_all_videos() -> list[dict]:
    """Return all completed parent videos with their subtopic children, newest first."""
    async with pool.acquire() as conn:
        # Get completed parents
        parents = await conn.fetch(
            """
            SELECT id, topic, status, created_at
            FROM videos
            WHERE parent_id IS NULL AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 50;
            """
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
                # Legacy single-video row
                results.append({
                    "id": p["id"],
                    "topic": p["topic"],
                    "created_at": p["created_at"],
                    "videos": [{"subtopic_title": p["topic"],
                                "video_url": p["video_url"],
                                "subtopic_index": 0}],
                })
        return results
