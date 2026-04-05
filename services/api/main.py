import asyncio, os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import google.auth.transport.requests
import google.oauth2.id_token
from database import (
    init_db, check_semantic_cache, create_session,
    create_subtopic_record, update_subtopic_record,
    mark_subtopic_failed, complete_parent_session,
    mark_failed, get_all_videos,
)
from embeddings import generate_embedding

AGENTS_URL = os.getenv("AGENTS_SERVICE_URL")
sessions: dict[str, dict] = {}


def _get_auth_token(audience: str) -> str:
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, audience)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="SketchMind API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class TopicRequest(BaseModel):
    topic: str


@app.post("/api/generate")
async def generate_video(req: TopicRequest):
    embedding = await generate_embedding(req.topic)

    # Semantic cache check — now returns multiple videos
    cached = await check_semantic_cache(embedding)
    if cached:
        return {
            "status": "cached",
            "videos": cached["videos"],
            "topic": cached["topic"],
        }

    video_id = await create_session(req.topic, embedding)
    sessions[video_id] = {"stage": "starting", "topic": req.topic}
    asyncio.create_task(run_pipeline(video_id, req.topic, embedding))
    return {"status": "processing", "session_id": video_id}


@app.websocket("/ws/status/{session_id}")
async def status_ws(ws: WebSocket, session_id: str):
    await ws.accept()
    try:
        while True:
            state = sessions.get(session_id, {"stage": "unknown"})
            await ws.send_json(state)
            if state["stage"] in ("completed", "failed"):
                break
            await asyncio.sleep(1.5)
    except WebSocketDisconnect:
        pass


async def run_pipeline(video_id: str, topic: str, embedding: list):
    try:
        sessions[video_id]["stage"] = "agents_working"

        headers = {}
        if AGENTS_URL and "run.app" in AGENTS_URL:
            headers["Authorization"] = f"Bearer {_get_auth_token(AGENTS_URL)}"

        resp = await asyncio.to_thread(
            lambda: httpx.post(
                f"{AGENTS_URL}/generate",
                json={"topic": topic, "session_id": video_id},
                headers=headers, timeout=300,
            )
        )
        result = resp.json()

        videos = result.get("videos", [])

        # Create child DB records for each subtopic
        for v in videos:
            child_id = await create_subtopic_record(
                parent_id=video_id,
                subtopic_title=v.get("subtopic_title", "Untitled"),
                subtopic_index=v.get("index", 0),
            )
            if v.get("video_url"):
                await update_subtopic_record(child_id, v["video_url"])
            elif v.get("error"):
                await mark_subtopic_failed(child_id, v["error"])
            else:
                await mark_subtopic_failed(child_id, "No video produced")

        await complete_parent_session(video_id)

        succeeded = [v for v in videos if v.get("video_url")]
        if succeeded:
            sessions[video_id] = {
                "stage": "completed",
                "videos": videos,
            }
        else:
            sessions[video_id] = {
                "stage": "failed",
                "error": result.get("error", "All subtopic videos failed"),
                "videos": videos,
            }

    except Exception as e:
        await mark_failed(video_id, str(e))
        sessions[video_id] = {"stage": "failed", "error": str(e)}


@app.get("/api/videos")
async def list_videos():
    return await get_all_videos()


@app.get("/health")
async def health():
    return {"status": "ok"}
