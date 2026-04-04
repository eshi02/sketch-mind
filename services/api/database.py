"""AlloyDB (PostgreSQL) database layer with pgvector semantic cache."""
import os, uuid
import asyncpg

ALLOYDB_HOST = os.getenv("ALLOYDB_HOST", "127.0.0.1")
ALLOYDB_PORT = int(os.getenv("ALLOYDB_PORT", "5432"))
ALLOYDB_DB = os.getenv("ALLOYDB_DB", "sketchmind")
ALLOYDB_USER = os.getenv("ALLOYDB_USER", "postgres")
ALLOYDB_PASS = os.getenv("ALLOYDB_PASS", "changeme")

SIMILARITY_THRESHOLD = 0.85

pool: asyncpg.Pool | None = None


async def init_db():
    """Create connection pool and ensure tables + pgvector extension exist."""
    global pool
    pool = await asyncpg.create_pool(
        host=ALLOYDB_HOST, port=ALLOYDB_PORT, database=ALLOYDB_DB,
        user=ALLOYDB_USER, password=ALLOYDB_PASS,
        min_size=2, max_size=10,
    )
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
            CREATE INDEX IF NOT EXISTS idx_videos_embedding
            ON videos USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 10);
        """)


async def check_semantic_cache(embedding: list[float]) -> dict | None:
    """Return a completed video if a semantically similar topic exists."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, topic, video_url,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM videos
            WHERE status = 'completed' AND video_url IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 1;
            """,
            str(embedding),
        )
        if row and row["similarity"] >= SIMILARITY_THRESHOLD:
            return {"id": row["id"], "topic": row["topic"],
                    "video_url": row["video_url"],
                    "similarity": float(row["similarity"])}
    return None


async def create_session(topic: str, embedding: list[float]) -> str:
    """Insert a new video record and return its id."""
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


async def update_video_record(video_id: str, video_url: str):
    """Mark a video as completed with its URL."""
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
    """Return all completed videos, newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, topic, video_url, status, created_at
            FROM videos
            WHERE status = 'completed' AND video_url IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 50;
            """
        )
        return [dict(r) for r in rows]
