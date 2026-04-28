"""SketchMind API gateway — FastAPI service that orchestrates video generation."""

import asyncio
import logging
import os
import warnings
from contextlib import asynccontextmanager

warnings.filterwarnings("ignore", category=UserWarning, module="vertexai")

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
    append_path_topic,
    attach_session_to_path_topic,
    backfill_path_embeddings,
    check_path_outline_cache,
    check_semantic_cache,
    find_active_session_by_topic,
    find_completed_parent_by_topic,
    get_quiz,
    get_session_status,
    get_session_topic_and_subtopics,
    save_quiz,
    clear_user_history,
    create_learning_path,
    create_session,
    create_subtopic_record,
    delete_learning_path,
    delete_search_history,
    find_user_path_by_embedding,
    find_user_path_by_exact_title,
    get_all_videos,
    get_learning_path,
    get_user_history,
    get_user_paths,
    init_db,
    mark_failed,
    mark_path_topic_completed,
    reconcile_stale_history,
    update_session_stage,
    upsert_google_user,
)
from cloud_tasks import enqueue_subtopic_task
from embeddings import (
    generate_embedding,
    generate_path_outline,
    generate_quiz,
    normalize_topic,
)

logger = logging.getLogger(__name__)

AGENTS_URL = os.getenv("AGENTS_SERVICE_URL")

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
    # Embed any pre-existing learning_paths so L2 fuzzy dedupe can see them.
    await backfill_path_embeddings(generate_embedding)
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


class CreatePathRequest(BaseModel):
    title: str
    topics: list[str] = []


class AddPathTopicRequest(BaseModel):
    topic: str


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
# Learning paths
# ---------------------------------------------------------------------------


@app.post("/api/paths")
async def create_path(req: CreatePathRequest, request: Request):
    """Create a new learning path. If `topics` is empty, the AI breaks the
    title into a structured syllabus automatically.

    Layered to minimise LLM credits:
      L0 exact-title dedupe (SQL)        → return existing
      L1 embed once
      L2 fuzzy per-user dedupe (pgvector) → return existing
      L3 cross-user syllabus cache       → reuse topic list
      L4 generate_path_outline (Gemini)  → fresh syllabus
    """
    user = get_current_user(request)
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    # L0: exact-title dedupe, no LLM cost.
    existing_id = await find_user_path_by_exact_title(user["sub"], title)
    if existing_id:
        existing = await get_learning_path(existing_id, user["sub"])
        if existing:
            logger.info("Path dedupe (L0 exact): user=%s → %s", user["sub"], existing_id)
            return existing

    topics = [t.strip() for t in req.topics if t.strip()]

    # L1: embed once. L2: fuzzy per-user dedupe.
    embedding = await generate_embedding(title)
    existing_id = await find_user_path_by_embedding(user["sub"], embedding)
    if existing_id:
        existing = await get_learning_path(existing_id, user["sub"])
        if existing:
            logger.info("Path dedupe (L2 fuzzy): user=%s → %s", user["sub"], existing_id)
            return existing

    if not topics:
        # L3: reuse another user's syllabus before falling back to Gemini.
        cached = await check_path_outline_cache(embedding)
        if cached:
            topics = cached
            logger.info("Path syllabus reused from cache (L3) for %r", title)
        else:
            try:
                topics = await generate_path_outline(title)
            except Exception as exc:
                logger.error("Path outline generation failed for %r: %s", title, exc)
                raise HTTPException(
                    status_code=500,
                    detail="Could not generate topics for this title. Try adding topics manually.",
                )
            if not topics:
                raise HTTPException(
                    status_code=500,
                    detail="AI returned no topics. Try a different title or add topics manually.",
                )

    if len(topics) > 20:
        raise HTTPException(status_code=400, detail="A path can have at most 20 topics")

    path_id = generate_id()
    return await create_learning_path(
        path_id, user["sub"], title, topics, embedding=embedding,
    )


@app.post("/api/paths/{path_id}/topics")
async def add_path_topic(path_id: str, req: AddPathTopicRequest, request: Request):
    """Append a new topic to an existing learning path."""
    user = get_current_user(request)
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")
    existing = await get_learning_path(path_id, user["sub"])
    if existing is None:
        raise HTTPException(status_code=404, detail="Path not found")
    if len(existing["topics"]) >= 20:
        raise HTTPException(status_code=400, detail="A path can have at most 20 topics")
    result = await append_path_topic(path_id, user["sub"], topic)
    return result or {"topics": existing["topics"]}


@app.get("/api/paths")
async def list_paths(request: Request):
    """List all learning paths for the authenticated user."""
    user = get_current_user(request)
    return await get_user_paths(user["sub"])


@app.get("/api/paths/{path_id}")
async def get_path(path_id: str, request: Request):
    """Get a single path with attached video data per topic."""
    user = get_current_user(request)
    path = await get_learning_path(path_id, user["sub"])
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")
    return path


async def _kick_off_path_topic(
    path_id: str, user_id: str, index: int, topic_entry: dict,
) -> dict:
    """Internal helper: start (or attach cache for) one topic in a path.

    Returns a dict with status + session_id. Idempotent: if topic already has
    a session_id, returns it unchanged. Used for both the user-initiated
    start and the silent pre-fetch of the next topic.
    """
    if topic_entry.get("session_id"):
        return {"status": "existing", "session_id": topic_entry["session_id"]}

    topic = topic_entry["topic"]

    # Fast-path: exact-text match against completed parents. Catches the common
    # case where two users share a path topic title verbatim (the L3 syllabus
    # cache copies titles unchanged), with zero LLM cost — no normalize, no
    # embedding. Falls through to the existing semantic cache on miss.
    exact_hit = await find_completed_parent_by_topic(topic)
    if exact_hit:
        await attach_session_to_path_topic(path_id, user_id, index, exact_hit["id"])
        return {
            "status": "cached",
            "session_id": exact_hit["id"],
            "videos": exact_hit["videos"],
        }

    normalized = await normalize_topic(topic)
    embedding = await generate_embedding(normalized)

    cached = await check_semantic_cache(embedding)
    if cached:
        await attach_session_to_path_topic(path_id, user_id, index, cached["id"])
        return {"status": "cached", "session_id": cached["id"], "videos": cached["videos"]}

    # Reuse in-flight session for the same topic if one is running.
    in_flight = await find_active_session_by_topic(topic)
    if in_flight:
        await attach_session_to_path_topic(path_id, user_id, index, in_flight)
        return {"status": "processing", "session_id": in_flight}

    video_id = await create_session(topic, embedding)
    await attach_session_to_path_topic(path_id, user_id, index, video_id)

    # Pass user_id=None so the pipeline does NOT write to search_history.
    # Path-generated videos are already visible in the path roadmap; only
    # explicit single-topic searches from the home page belong in history.
    asyncio.create_task(run_pipeline(video_id, topic, user_id=None))
    return {"status": "processing", "session_id": video_id}


async def _prefetch_next_topic(
    path_id: str, user_id: str, next_index: int, topic_entry: dict,
) -> None:
    """Silently start generation for the next topic. Errors are swallowed —
    pre-fetch is best-effort; the user will retry via Start if it failed."""
    try:
        await _kick_off_path_topic(path_id, user_id, next_index, topic_entry)
    except Exception as exc:
        logger.warning(
            "Pre-fetch failed for path=%s index=%d: %s", path_id, next_index, exc,
        )


@app.post("/api/paths/{path_id}/start/{index}")
async def start_path_topic(path_id: str, index: int, request: Request):
    """Trigger video generation for the topic at the given index in the path.

    Also pre-fetches the next topic in the background so it's ready when the
    user advances. The pre-fetched topic stays locked in the UI; only its
    generation runs eagerly.
    """
    user = get_current_user(request)
    path = await get_learning_path(path_id, user["sub"])
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")
    if index < 0 or index >= len(path["topics"]):
        raise HTTPException(status_code=400, detail="Invalid topic index")
    if index > path["current_index"]:
        raise HTTPException(status_code=403, detail="Previous topics must be completed first")

    result = await _kick_off_path_topic(
        path_id, user["sub"], index, path["topics"][index],
    )

    # 2-deep prefetch lookahead — N+1 and N+2 both kick off in parallel,
    # so by the time the user advances twice the second-next topic has had
    # plenty of head-start. Subsequent Starts re-trigger the same prefetch
    # but the session_id check below is a no-op once a session exists.
    for offset in (1, 2):
        nx = index + offset
        if (
            nx < len(path["topics"])
            and not path["topics"][nx].get("session_id")
        ):
            asyncio.create_task(_prefetch_next_topic(
                path_id, user["sub"], nx, path["topics"][nx],
            ))

    return result


@app.delete("/api/paths/{path_id}")
async def delete_path(path_id: str, request: Request):
    """Delete a learning path."""
    user = get_current_user(request)
    deleted = await delete_learning_path(path_id, user["sub"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Path not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Path-topic quizzes (gate completion at >= 80% score)
# ---------------------------------------------------------------------------

QUIZ_PASS_THRESHOLD = 0.8  # 4 of 5 by default


def _strip_answers(questions: list[dict]) -> list[dict]:
    """Strip `correct` and `explain` from a quiz before returning to the
    client at fetch time, so the answer key never leaks pre-submission."""
    return [
        {"q": q["q"], "options": q["options"]}
        for q in questions
    ]


async def _resolve_path_topic(
    path_id: str, index: int, user_id: str,
) -> tuple[dict, dict]:
    """Verify the user owns the path and topic exists. Returns (path, topic)."""
    path = await get_learning_path(path_id, user_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")
    if index < 0 or index >= len(path["topics"]):
        raise HTTPException(status_code=400, detail="Invalid topic index")
    topic_entry = path["topics"][index]
    if not topic_entry.get("session_id"):
        raise HTTPException(
            status_code=400,
            detail="Topic has no videos yet — start the topic first.",
        )
    return path, topic_entry


@app.get("/api/paths/{path_id}/quiz/{index}")
async def get_path_topic_quiz(path_id: str, index: int, request: Request):
    """Return the quiz for a path topic. Lazy-generates + caches per
    session_id, so cross-user shares are free after the first generation."""
    user = get_current_user(request)
    _, topic_entry = await _resolve_path_topic(path_id, index, user["sub"])
    session_id = topic_entry["session_id"]

    questions = await get_quiz(session_id)
    if questions is None:
        ctx = await get_session_topic_and_subtopics(session_id)
        if not ctx:
            raise HTTPException(
                status_code=404, detail="Video session context unavailable.",
            )
        try:
            questions = await generate_quiz(ctx["topic"], ctx["subtopics"])
        except Exception as exc:
            logger.error(
                "Quiz generation failed for session=%s: %s", session_id, exc,
            )
            raise HTTPException(
                status_code=500,
                detail="Could not generate the quiz. Try again in a moment.",
            )
        if not questions:
            raise HTTPException(
                status_code=500,
                detail="Quiz generator returned no valid questions.",
            )
        await save_quiz(session_id, questions)

    return {
        "session_id": session_id,
        "topic": topic_entry["topic"],
        "questions": _strip_answers(questions),
        "pass_threshold": QUIZ_PASS_THRESHOLD,
    }


class QuizSubmitRequest(BaseModel):
    answers: list[int]


@app.post("/api/paths/{path_id}/quiz/{index}/submit")
async def submit_path_topic_quiz(
    path_id: str, index: int, req: QuizSubmitRequest, request: Request,
):
    """Grade a quiz submission server-side. If score >= QUIZ_PASS_THRESHOLD,
    also calls `mark_path_topic_completed` to mark the topic complete and
    unlock the next one. This is the only path to topic completion."""
    user = get_current_user(request)
    _, topic_entry = await _resolve_path_topic(path_id, index, user["sub"])
    session_id = topic_entry["session_id"]

    questions = await get_quiz(session_id)
    if questions is None:
        raise HTTPException(
            status_code=400,
            detail="Fetch the quiz first before submitting.",
        )
    if len(req.answers) != len(questions):
        raise HTTPException(
            status_code=400,
            detail=f"Expected {len(questions)} answers, got {len(req.answers)}.",
        )

    correct_count = 0
    per_question = []
    for q, a in zip(questions, req.answers):
        is_correct = isinstance(a, int) and a == q["correct"]
        if is_correct:
            correct_count += 1
        per_question.append({
            "correct_index": q["correct"],
            "user_index": a,
            "is_correct": is_correct,
            "explain": q.get("explain", ""),
        })

    score = correct_count / len(questions) if questions else 0.0
    passed = score >= QUIZ_PASS_THRESHOLD

    advanced = None
    if passed:
        advanced = await mark_path_topic_completed(path_id, user["sub"], index)
        # Belt-and-braces lookahead: when a topic completes and the next
        # one becomes "current", make sure the topic *after* it has its
        # generation kicked off too. Skips no-op when already running.
        if advanced:
            after = advanced["current_index"] + 1
            topics_now = advanced["topics"]
            if (
                after < len(topics_now)
                and not topics_now[after].get("session_id")
            ):
                asyncio.create_task(_prefetch_next_topic(
                    path_id, user["sub"], after, topics_now[after],
                ))

    return {
        "score": score,
        "correct_count": correct_count,
        "total": len(questions),
        "passed": passed,
        "pass_threshold": QUIZ_PASS_THRESHOLD,
        "per_question": per_question,
        "current_index": advanced["current_index"] if advanced else None,
    }


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

    # Prevent duplicate in-flight generations for the same topic.
    in_flight = await find_active_session_by_topic(req.topic)
    if in_flight:
        return {"status": "processing", "session_id": in_flight}

    video_id = await create_session(req.topic, embedding)

    user_id = user["sub"] if user else None
    asyncio.create_task(run_pipeline(video_id, req.topic, user_id=user_id))
    return {"status": "processing", "session_id": video_id}


@app.websocket("/ws/status/{session_id}")
async def status_ws(ws: WebSocket, session_id: str):
    """Stream generation status updates to the frontend by polling the DB
    (replaces the old in-memory `sessions` dict — survives API restarts and
    works across multiple API instances)."""
    await ws.accept()
    try:
        while True:
            state = await get_session_status(session_id)
            if state is None:
                await ws.send_json({"stage": "unknown"})
                break
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
    """Phase 1: research subtopics. Phase 2: enqueue subtopic tasks.

    Each subtopic processing task is dispatched via Cloud Tasks (or direct HTTP
    fallback). Per-subtopic stage updates are written to the DB by the agents
    service. Parent finalization (status='completed'/'failed' + history write)
    is performed by the agents endpoint when the last subtopic settles, since
    this background coroutine returns as soon as enqueue finishes.
    """
    try:
        await update_session_stage(video_id, "researching", "Researching subtopics...")

        async with httpx.AsyncClient(timeout=300, headers=_auth_headers()) as client:
            resp = await client.post(
                f"{AGENTS_URL}/research", json={"topic": topic},
            )
        research_result = resp.json()

        if research_result.get("status") == "error" or not research_result.get("subtopics"):
            error = research_result.get("error", "No subtopics generated")
            await mark_failed(video_id, error)
            return

        subtopics = research_result["subtopics"]

        # Insert one child row per subtopic so the WebSocket sees the full
        # ordered list immediately in `pending` state.
        for i, st in enumerate(subtopics):
            title = st.get("subtopic_title", f"Subtopic {i + 1}")
            await create_subtopic_record(
                parent_id=video_id, subtopic_title=title, subtopic_index=i,
            )

        await update_session_stage(video_id, "generating", "Generating videos...")

        # Enqueue each subtopic. Cloud Tasks dispatches them at the queue's
        # configured concurrency (default 8); the agents service finalizes the
        # parent when the last subtopic completes.
        for i, st in enumerate(subtopics):
            await enqueue_subtopic_task(
                video_id=video_id,
                subtopic_data={**st, "user_id": user_id, "topic": topic},
                index=i,
            )

    except Exception as exc:
        logger.exception("run_pipeline failed for video_id=%s: %s", video_id, exc)
        await mark_failed(video_id, str(exc))


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
