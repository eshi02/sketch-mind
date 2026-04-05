"""One-time migration: replace IVFFlat index with HNSW.

Run once, then delete this file:
    python migrate_index.py
"""
import asyncio, os
import asyncpg


async def migrate():
    conn = await asyncpg.connect(
        host=os.getenv("ALLOYDB_HOST", "127.0.0.1"),
        port=int(os.getenv("ALLOYDB_PORT", "5432")),
        database=os.getenv("ALLOYDB_DB", "sketchmind"),
        user=os.getenv("ALLOYDB_USER", "postgres"),
        password=os.getenv("ALLOYDB_PASS", "changeme"),
    )
    try:
        await conn.execute("DROP INDEX IF EXISTS idx_videos_embedding;")
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_embedding_hnsw
            ON videos USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """)
        print("Done: dropped IVFFlat index, created HNSW index.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
