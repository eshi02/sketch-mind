"""Agent service: research endpoint + streaming subtopic endpoint."""
import json, re, logging, time
from google.adk.tools.mcp_tool import McpToolset
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import create_agents

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sketchmind-agents")

# Suppress noisy ADK/Gemini request/response logs
logging.getLogger("google_adk").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="SketchMind Agents")

# Map ADK agent names to user-friendly stage descriptions
AGENT_STAGES = {
    "scriptwriter": {"stage": "scripting", "message": "Writing video script..."},
    "manim_generator": {"stage": "coding", "message": "Generating animation code..."},
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
    researcher, _, mcp_toolset = await create_agents()
    try:
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

        try:
            subtopics = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            m = re.search(r'\[.*\]', str(raw), re.DOTALL)
            subtopics = json.loads(m.group(0)) if m else []

        if not isinstance(subtopics, list) or len(subtopics) == 0:
            return {"status": "error", "error": "No subtopics generated", "subtopics": []}

        return {"status": "ok", "subtopics": subtopics}
    finally:
        await mcp_toolset.close()


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

        # Each subtopic gets its own agents + MCP subprocess to avoid
        # shared-state and stdio-pipe contention between concurrent runs.
        _, subtopic_pipeline, mcp_toolset = await create_agents()
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
        finally:
            await mcp_toolset.close()

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@app.get("/health")
async def health():
    return {"status": "ok", "agents": ["researcher", "subtopic_pipeline"]}
