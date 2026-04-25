"""SketchMind API gateway — FastAPI service that orchestrates video generation."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
import google.auth.transport.requests
import google.oauth2.id_token
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from auth import (
    create_token,
    generate_id,
    get_current_user,
    get_optional_user,
    verify_google_token,
)
from database import (
    add_search_history,
    check_semantic_cache,
    clear_user_history,
    complete_parent_session,
    create_session,
    create_subtopic_record,
    delete_search_history,
    get_all_videos,
    get_user_history,
    init_db,
    mark_failed,
    mark_subtopic_failed,
    reconcile_stale_history,
    update_subtopic_record,
    upsert_google_user,
)
from embeddings import generate_embedding, normalize_topic

logger = logging.getLogger(__name__)

AGENTS_URL = os.getenv("AGENTS_SERVICE_URL")

# In-memory session state for WebSocket polling (lost on restart).
sessions: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Service-to-service auth
# ---------------------------------------------------------------------------


def _get_auth_token(audience: str) -> str:
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, audience)


def _auth_headers() -> dict:
    """Return Authorization header for Cloud Run service-to-service calls."""
    headers: dict[str, str] = {}
    if AGENTS_URL and "run.app" in AGENTS_URL:
        headers["Authorization"] = f"Bearer {_get_auth_token(AGENTS_URL)}"
    return headers


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    await reconcile_stale_history()
    yield


app = FastAPI(title="SketchMind API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class TopicRequest(BaseModel):
    topic: str


class GoogleAuthRequest(BaseModel):
    id_token: str


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@app.post("/api/auth/google")
async def google_auth(req: GoogleAuthRequest):
    """Verify Google ID token, upsert user, return our JWT."""
    google_user = verify_google_token(req.id_token)
    user = await upsert_google_user(
        user_id=generate_id(),
        google_id=google_user["google_id"],
        name=google_user["name"],
        email=google_user["email"],
        avatar_url=google_user["picture"],
    )
    token = create_token(user["id"], user["email"], user["name"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "avatar_url": user.get("avatar_url", ""),
        },
    }


@app.get("/api/auth/me")
async def get_me(request: Request):
    """Return current user info from JWT."""
    payload = get_current_user(request)
    return {"id": payload["sub"], "name": payload["name"], "email": payload["email"]}


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------


@app.get("/api/history")
async def history(request: Request):
    """Return search history for the authenticated user."""
    user = get_current_user(request)
    return await get_user_history(user["sub"])


@app.delete("/api/history/{history_id}")
async def delete_history(history_id: str, request: Request):
    """Delete a single history entry."""
    user = get_current_user(request)
    deleted = await delete_search_history(history_id, user["sub"])
    if not deleted:
        raise HTTPException(status_code=404, detail="History entry not found")
    return {"ok": True}


@app.delete("/api/history")
async def clear_history(request: Request):
    """Delete all history for the authenticated user."""
    user = get_current_user(request)
    await clear_user_history(user["sub"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------


@app.post("/api/generate")
async def generate_video(req: TopicRequest, request: Request):
    """Start video generation or return cached result."""
    normalized = await normalize_topic(req.topic)
    embedding = await generate_embedding(normalized)
    user = get_optional_user(request)

    cached = await check_semantic_cache(embedding)
    if cached:
        if user:
            h_id = generate_id()
            await add_search_history(
                h_id, user["sub"], req.topic, cached.get("id"), status="completed",
            )
        return {
            "status": "cached",
            "videos": cached["videos"],
            "topic": cached["topic"],
        }

    video_id = await create_session(req.topic, embedding)
    sessions[video_id] = {"stage": "starting", "topic": req.topic, "subtopics": []}

    user_id = user["sub"] if user else None
    asyncio.create_task(run_pipeline(video_id, req.topic, user_id=user_id))
    return {"status": "processing", "session_id": video_id}


@app.websocket("/ws/status/{session_id}")
async def status_ws(ws: WebSocket, session_id: str):
    """Stream generation status updates to the frontend."""
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


async def run_pipeline(video_id: str, topic: str, user_id: str | None = None) -> None:
    """Phase 1: research subtopics. Phase 2: parallel subtopic processing."""
    try:
        sessions[video_id]["stage"] = "researching"

        async with httpx.AsyncClient(timeout=300, headers=_auth_headers()) as client:
            resp = await client.post(
                f"{AGENTS_URL}/research", json={"topic": topic},
            )
        research_result = resp.json()

        if research_result.get("status") == "error" or not research_result.get("subtopics"):
            error = research_result.get("error", "No subtopics generated")
            await mark_failed(video_id, error)
            sessions[video_id] = {"stage": "failed", "error": error, "subtopics": []}
            return

        subtopics = research_result["subtopics"]

        subtopic_states = [
            {
                "subtopic_title": st.get("subtopic_title", f"Subtopic {i + 1}"),
                "index": i,
                "stage": "pending",
                "message": "Waiting...",
                "video_url": None,
                "error": None,
            }
            for i, st in enumerate(subtopics)
        ]
        sessions[video_id] = {"stage": "generating", "subtopics": subtopic_states}

        tasks = [
            _process_single_subtopic(video_id, st, i)
            for i, st in enumerate(subtopics)
        ]
        await asyncio.gather(*tasks)

        await complete_parent_session(video_id)

        final_subtopics = sessions[video_id]["subtopics"]
        has_video = any(s.get("video_url") for s in final_subtopics)
        sessions[video_id]["stage"] = "completed" if has_video else "failed"
        if not has_video:
            sessions[video_id]["error"] = "All subtopic videos failed"

        if user_id and has_video:
            h_id = generate_id()
            await add_search_history(h_id, user_id, topic, video_id, status="completed")

    except Exception as exc:
        await mark_failed(video_id, str(exc))
        sessions[video_id] = {"stage": "failed", "error": str(exc), "subtopics": []}


async def _process_single_subtopic(
    video_id: str, subtopic_data: dict, index: int,
) -> None:
    """Stream NDJSON from agents /process-subtopic, updating sessions dict live."""
    title = subtopic_data.get("subtopic_title", f"Subtopic {index + 1}")
    child_id = await create_subtopic_record(
        parent_id=video_id, subtopic_title=title, subtopic_index=index,
    )

    try:
        timeouts = httpx.Timeout(connect=30, read=600, write=30, pool=60)
        async with httpx.AsyncClient(timeout=timeouts, headers=_auth_headers()) as client:
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

                    st = sessions[video_id]["subtopics"][index]
                    st["stage"] = event.get("stage", st["stage"])
                    if "message" in event:
                        st["message"] = event["message"]

                    if event.get("stage") in ("completed", "failed"):
                        st["video_url"] = event.get("video_url")
                        st["error"] = event.get("error")

                        if event.get("video_url"):
                            await update_subtopic_record(child_id, event["video_url"])
                        else:
                            await mark_subtopic_failed(
                                child_id, event.get("error", "No video"),
                            )

    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        error_msg = f"Timeout waiting for agents service: {type(exc).__name__}"
        logger.error("Subtopic [%d] %r: %s", index, title, error_msg)
        sessions[video_id]["subtopics"][index].update({
            "stage": "failed", "message": error_msg, "error": error_msg,
        })
        await mark_subtopic_failed(child_id, error_msg)
    except Exception as exc:
        error_msg = str(exc)
        sessions[video_id]["subtopics"][index].update({
            "stage": "failed", "message": error_msg, "error": error_msg,
        })
        await mark_subtopic_failed(child_id, error_msg)


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------


@app.get("/api/videos")
async def list_videos():
    """Return all completed videos (public gallery)."""
    return await get_all_videos()


@app.get("/health")
async def health():
    return {"status": "ok"}
