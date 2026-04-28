"""Agent service: research endpoint + streaming subtopic endpoint."""
import json, re, logging, time, warnings

# authlib.deprecate calls simplefilter("always") at module level, overriding
# any prior filter. Import it first, then re-apply our ignore filter so it
# takes priority before the rest of the import chain triggers warnings.
import authlib.deprecate  # noqa: E402
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import create_agents
from db import close_db, finalize_if_all_done, init_db, update_subtopic_stage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sketchmind-agents")

# Suppress noisy ADK/Gemini library logs
logging.getLogger("google_adk").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build agents and MCP toolset once at startup, reuse for every request.

    Previously each /research and /process-subtopic call spawned its own MCP
    subprocess (~1-2s setup tax) and re-loaded the Manim API tools. With pooling,
    every request reads agent + toolset from app.state. Saves the spawn cost on
    every call and keeps the lru_cache in manim_api_server.py warm across all
    subtopics that hit the same agents instance.

    Also opens the DB pool used by /process-subtopic-task to write per-stage
    progress that the API's WebSocket reads.
    """
    researcher, subtopic_pipeline, mcp_toolset = await create_agents()
    app.state.researcher = researcher
    app.state.subtopic_pipeline = subtopic_pipeline
    app.state.mcp_toolset = mcp_toolset
    await init_db()
    logger.info("Agents service warmed up: MCP toolset, agent graph, DB pool ready")
    try:
        yield
    finally:
        await mcp_toolset.close()
        await close_db()


app = FastAPI(title="SketchMind Agents", lifespan=lifespan)

# Map ADK agent names to user-friendly stage descriptions
AGENT_STAGES = {
    "scriptwriter": {"stage": "scripting", "message": "Writing video script..."},
    "manim_generator": {"stage": "coding", "message": "Generating animation code..."},
    "narrator": {"stage": "narrating", "message": "Writing narration..."},
    "renderer": {"stage": "rendering", "message": "Rendering video..."},
    "manim_fixer": {"stage": "fixing", "message": "Fixing code, retrying render..."},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_video_url(text: str) -> str | None:
    match = re.search(r'https://storage\.googleapis\.com/\S+\.mp4', text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# POST /research — Phase 1: returns subtopic list
# ---------------------------------------------------------------------------

class ResearchRequest(BaseModel):
    topic: str


@app.post("/research")
async def research(req: ResearchRequest):
    """Run the researcher agent and return parsed subtopics."""
    researcher = app.state.researcher
    session_service = InMemorySessionService()
    runner = Runner(
        agent=researcher, app_name="sketchmind", session_service=session_service
    )
    session = await session_service.create_session(
        app_name="sketchmind", user_id="user"
    )

    logger.info(f"Research: topic={req.topic}")
    t0 = time.time()
    async for _ in runner.run_async(
        user_id="user",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(
                text=f"Create an educational video explaining: {req.topic}"
            )],
        ),
    ):
        pass
    logger.info(f"Research completed in {time.time() - t0:.1f}s")

    state = (await session_service.get_session(
        app_name="sketchmind", user_id="user", session_id=session.id
    )).state
    raw = state.get("CURRICULUM_JSON", "[]")
    logger.info(f"CURRICULUM_JSON: {str(raw)[:500]}")

    if isinstance(raw, str):
        # Escape bare backslashes that aren't valid JSON escapes (e.g. LaTeX
        # like \sin, \theta, \frac that the LLM puts in key_formulas).
        fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
        try:
            subtopics = json.loads(fixed)
        except json.JSONDecodeError:
            m = re.search(r'\[.*\]', fixed, re.DOTALL)
            try:
                subtopics = json.loads(m.group(0)) if m else []
            except (json.JSONDecodeError, AttributeError):
                subtopics = []
    else:
        subtopics = raw

    if not isinstance(subtopics, list) or len(subtopics) == 0:
        return {"status": "error", "error": "No subtopics generated", "subtopics": []}

    return {"status": "ok", "subtopics": subtopics}


# ---------------------------------------------------------------------------
# POST /process-subtopic — Phase 2: streams stage updates as NDJSON
# ---------------------------------------------------------------------------

class SubtopicRequest(BaseModel):
    subtopic_data: dict
    index: int = 0


@app.post("/process-subtopic")
async def process_subtopic(req: SubtopicRequest):
    """Process a single subtopic. Streams NDJSON lines with stage updates,
    ending with a final line containing video_url or error."""
    title = req.subtopic_data.get("subtopic_title", f"Subtopic {req.index + 1}")

    async def event_stream():
        # Emit initial stage
        yield json.dumps({"stage": "starting", "message": f"Processing: {title}"}) + "\n"

        # Reuse the agent + MCP toolset built once at startup. ADK Agents are
        # stateless config; per-request runtime state lives in InvocationContext.
        subtopic_pipeline = app.state.subtopic_pipeline
        try:
            session_service = InMemorySessionService()
            runner = Runner(
                agent=subtopic_pipeline,
                app_name="sketchmind",
                session_service=session_service,
            )
            session = await session_service.create_session(
                app_name="sketchmind", user_id="user"
            )
            session.state["SUBTOPIC_DATA"] = json.dumps(req.subtopic_data)

            final_text = ""
            video_url = None
            last_stage = None
            stage_start = time.time()
            pipeline_start = stage_start

            async for event in runner.run_async(
                user_id="user",
                session_id=session.id,
                new_message=types.Content(
                    role="user",
                    parts=[types.Part.from_text(
                        text=f"Create an educational video about: {title}"
                    )],
                ),
            ):
                # Emit stage changes based on which agent is active
                author = getattr(event, "author", "")
                if author in AGENT_STAGES and author != last_stage:
                    now = time.time()
                    if last_stage:
                        logger.info(f"[Subtopic {req.index}] {last_stage} took {now - stage_start:.1f}s")
                    stage_start = now
                    last_stage = author
                    yield json.dumps(AGENT_STAGES[author]) + "\n"

                # Capture video URL from function responses
                if hasattr(event, "content") and event.content:
                    for part in (event.content.parts or []):
                        if hasattr(part, "text") and part.text:
                            final_text = part.text
                        if hasattr(part, "function_response") and part.function_response:
                            resp = part.function_response.response
                            if isinstance(resp, dict) and resp.get("video_url"):
                                video_url = resp["video_url"]

            # Fallback: check session state
            if not video_url:
                state = (await session_service.get_session(
                    app_name="sketchmind", user_id="user", session_id=session.id
                )).state
                # VIDEO_URL is set directly by the deterministic RenderAgent on success.
                direct = state.get("VIDEO_URL")
                if direct:
                    video_url = str(direct)
                if not video_url:
                    for key in ["RENDER_ERROR", "RENDER_RESULT"]:
                        val = state.get(key, "")
                        if val:
                            video_url = _extract_video_url(str(val))
                            if video_url:
                                break
                if not video_url and final_text:
                    video_url = _extract_video_url(final_text)

            # Log final stage timing
            if last_stage:
                logger.info(f"[Subtopic {req.index}] {last_stage} took {time.time() - stage_start:.1f}s")
            logger.info(f"[Subtopic {req.index}] Total pipeline: {time.time() - pipeline_start:.1f}s — {'success' if video_url else 'failed'}")

            # Final result line
            yield json.dumps({
                "stage": "completed" if video_url else "failed",
                "subtopic_title": title,
                "video_url": video_url,
                "index": req.index,
                "error": None if video_url else "No video produced",
            }) + "\n"

        except Exception as e:
            logger.error(f"Subtopic [{req.index}] '{title}' failed: {e}")
            yield json.dumps({
                "stage": "failed",
                "subtopic_title": title,
                "video_url": None,
                "index": req.index,
                "error": str(e),
            }) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# POST /process-subtopic-task — Cloud Tasks target. Writes stage updates to DB.
# ---------------------------------------------------------------------------

class SubtopicTaskRequest(BaseModel):
    video_id: str
    subtopic_data: dict
    index: int = 0


@app.post("/process-subtopic-task")
async def process_subtopic_task(req: SubtopicTaskRequest):
    """Process one subtopic end-to-end. Writes per-stage progress to the
    `videos` table (read by the API's WebSocket). On the last sibling to
    settle, also finalizes the parent and writes search_history.

    Returns 200 on success, 500 on internal error so Cloud Tasks retries.
    """
    title = req.subtopic_data.get("subtopic_title", f"Subtopic {req.index + 1}")
    user_id = req.subtopic_data.get("user_id")
    parent_topic = req.subtopic_data.get("topic", title)

    logger.info(
        "[task] video_id=%s index=%d title=%r start",
        req.video_id, req.index, title,
    )

    try:
        await update_subtopic_stage(
            req.video_id, req.index, "starting", f"Processing: {title}",
        )

        subtopic_pipeline = app.state.subtopic_pipeline
        session_service = InMemorySessionService()
        runner = Runner(
            agent=subtopic_pipeline,
            app_name="sketchmind",
            session_service=session_service,
        )
        session = await session_service.create_session(
            app_name="sketchmind", user_id="user",
        )
        # Drop user_id/topic before handing to the agent — they're our metadata,
        # not part of the subtopic prompt.
        agent_subtopic_data = {
            k: v for k, v in req.subtopic_data.items()
            if k not in ("user_id", "topic")
        }
        session.state["SUBTOPIC_DATA"] = json.dumps(agent_subtopic_data)

        final_text = ""
        video_url = None
        last_stage = None
        stage_start = time.time()
        pipeline_start = stage_start

        async for event in runner.run_async(
            user_id="user",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(
                    text=f"Create an educational video about: {title}"
                )],
            ),
        ):
            author = getattr(event, "author", "")
            if author in AGENT_STAGES and author != last_stage:
                now = time.time()
                if last_stage:
                    logger.info(
                        "[task] video_id=%s index=%d stage=%s took %.1fs",
                        req.video_id, req.index, last_stage, now - stage_start,
                    )
                stage_start = now
                last_stage = author
                stage_info = AGENT_STAGES[author]
                await update_subtopic_stage(
                    req.video_id, req.index,
                    stage_info["stage"], stage_info["message"],
                )

            if hasattr(event, "content") and event.content:
                for part in (event.content.parts or []):
                    if hasattr(part, "text") and part.text:
                        final_text = part.text
                    if hasattr(part, "function_response") and part.function_response:
                        resp = part.function_response.response
                        if isinstance(resp, dict) and resp.get("video_url"):
                            video_url = resp["video_url"]

        if not video_url:
            state = (await session_service.get_session(
                app_name="sketchmind", user_id="user", session_id=session.id,
            )).state
            direct = state.get("VIDEO_URL")
            if direct:
                video_url = str(direct)
            if not video_url:
                for key in ["RENDER_ERROR", "RENDER_RESULT"]:
                    val = state.get(key, "")
                    if val:
                        video_url = _extract_video_url(str(val))
                        if video_url:
                            break
            if not video_url and final_text:
                video_url = _extract_video_url(final_text)

        if last_stage:
            logger.info(
                "[task] video_id=%s index=%d stage=%s took %.1fs",
                req.video_id, req.index, last_stage, time.time() - stage_start,
            )
        logger.info(
            "[task] video_id=%s index=%d total=%.1fs %s",
            req.video_id, req.index, time.time() - pipeline_start,
            "success" if video_url else "failed",
        )

        if video_url:
            await update_subtopic_stage(
                req.video_id, req.index,
                "completed", "Video ready", video_url=video_url,
            )
        else:
            await update_subtopic_stage(
                req.video_id, req.index,
                "failed", "No video produced", error="No video produced",
            )

        await finalize_if_all_done(req.video_id, user_id, parent_topic)

        return {
            "status": "ok",
            "video_url": video_url,
            "index": req.index,
        }

    except Exception as exc:
        logger.exception(
            "[task] video_id=%s index=%d failed: %s",
            req.video_id, req.index, exc,
        )
        # Persist failure so the WebSocket sees it, then 500 so Cloud Tasks
        # retries up to max_attempts; the final failed UPDATE will stick.
        try:
            await update_subtopic_stage(
                req.video_id, req.index,
                "failed", "Processing failed", error=str(exc),
            )
            await finalize_if_all_done(req.video_id, user_id, parent_topic)
        except Exception:
            logger.exception("Failed to record subtopic failure")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
async def health():
    return {"status": "ok", "agents": ["researcher", "subtopic_pipeline"]}
