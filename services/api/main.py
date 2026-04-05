import asyncio, os, json
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


def _auth_headers() -> dict:
    headers = {}
    if AGENTS_URL and "run.app" in AGENTS_URL:
        headers["Authorization"] = f"Bearer {_get_auth_token(AGENTS_URL)}"
    return headers


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

    cached = await check_semantic_cache(embedding)
    if cached:
        return {
            "status": "cached",
            "videos": cached["videos"],
            "topic": cached["topic"],
        }

    video_id = await create_session(req.topic, embedding)
    sessions[video_id] = {"stage": "starting", "topic": req.topic, "subtopics": []}
    asyncio.create_task(run_pipeline(video_id, req.topic))
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
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(video_id: str, topic: str):
    """Phase 1: research. Phase 2: parallel subtopic processing with live updates."""
    try:
        # Phase 1: get subtopics from researcher
        sessions[video_id]["stage"] = "researching"

        async with httpx.AsyncClient(timeout=300, headers=_auth_headers()) as client:
            resp = await client.post(
                f"{AGENTS_URL}/research", json={"topic": topic}
            )
        research_result = resp.json()

        if research_result.get("status") == "error" or not research_result.get("subtopics"):
            error = research_result.get("error", "No subtopics generated")
            await mark_failed(video_id, error)
            sessions[video_id] = {"stage": "failed", "error": error, "subtopics": []}
            return

        subtopics = research_result["subtopics"]

        # Initialize per-subtopic state
        subtopic_states = []
        for i, st in enumerate(subtopics):
            title = st.get("subtopic_title", f"Subtopic {i + 1}")
            subtopic_states.append({
                "subtopic_title": title,
                "index": i,
                "stage": "pending",
                "message": "Waiting...",
                "video_url": None,
                "error": None,
            })
        sessions[video_id] = {"stage": "generating", "subtopics": subtopic_states}

        # Phase 2: process each subtopic in parallel
        tasks = [
            _process_single_subtopic(video_id, st, i)
            for i, st in enumerate(subtopics)
        ]
        await asyncio.gather(*tasks)

        # Finalize parent record
        await complete_parent_session(video_id)

        final_subtopics = sessions[video_id]["subtopics"]
        has_video = any(s.get("video_url") for s in final_subtopics)
        sessions[video_id]["stage"] = "completed" if has_video else "failed"
        if not has_video:
            sessions[video_id]["error"] = "All subtopic videos failed"

    except Exception as e:
        await mark_failed(video_id, str(e))
        sessions[video_id] = {"stage": "failed", "error": str(e), "subtopics": []}


async def _process_single_subtopic(video_id: str, subtopic_data: dict, index: int):
    """Stream NDJSON from agents /process-subtopic, updating sessions dict live."""
    title = subtopic_data.get("subtopic_title", f"Subtopic {index + 1}")

    # Create child DB record
    child_id = await create_subtopic_record(
        parent_id=video_id, subtopic_title=title, subtopic_index=index
    )

    try:
        async with httpx.AsyncClient(timeout=600, headers=_auth_headers()) as client:
            async with client.stream(
                "POST",
                f"{AGENTS_URL}/process-subtopic",
                json={"subtopic_data": subtopic_data, "index": index},
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Update the per-subtopic state in sessions dict
                    st = sessions[video_id]["subtopics"][index]
                    st["stage"] = event.get("stage", st["stage"])
                    if "message" in event:
                        st["message"] = event["message"]

                    # Final event contains video_url or error
                    if event.get("stage") in ("completed", "failed"):
                        st["video_url"] = event.get("video_url")
                        st["error"] = event.get("error")

                        if event.get("video_url"):
                            await update_subtopic_record(child_id, event["video_url"])
                        else:
                            await mark_subtopic_failed(
                                child_id, event.get("error", "No video")
                            )

    except Exception as e:
        sessions[video_id]["subtopics"][index].update({
            "stage": "failed",
            "message": str(e),
            "error": str(e),
        })
        await mark_subtopic_failed(child_id, str(e))


@app.get("/api/videos")
async def list_videos():
    return await get_all_videos()


@app.get("/health")
async def health():
    return {"status": "ok"}
