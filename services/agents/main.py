"""Agent service: two-phase orchestrator — research then parallel subtopic pipelines."""
import asyncio, json, re, logging
from fastapi import FastAPI
from pydantic import BaseModel
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import researcher, subtopic_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sketchmind-agents")

app = FastAPI(title="SketchMind Agents")


class GenerateRequest(BaseModel):
    topic: str
    session_id: str = "default"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_video_url(text: str) -> str | None:
    """Pull a GCS .mp4 URL out of a string."""
    match = re.search(r'https://storage\.googleapis\.com/\S+\.mp4', text)
    return match.group(0) if match else None


async def _run_researcher(topic: str) -> list[dict]:
    """Phase 1: run the researcher agent and return parsed subtopics list."""
    session_service = InMemorySessionService()
    runner = Runner(
        agent=researcher, app_name="sketchmind", session_service=session_service
    )
    session = await session_service.create_session(
        app_name="sketchmind", user_id="user"
    )

    logger.info(f"Phase 1: researching topic: {topic}")
    async for event in runner.run_async(
        user_id="user",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(
                text=f"Create an educational video explaining: {topic}"
            )],
        ),
    ):
        pass  # just drive to completion

    state = (await session_service.get_session(
        app_name="sketchmind", user_id="user", session_id=session.id
    )).state
    raw = state.get("CURRICULUM_JSON", "[]")
    logger.info(f"CURRICULUM_JSON raw: {str(raw)[:500]}")

    # Parse — the LLM should return raw JSON but may wrap it
    try:
        subtopics = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        # Try to extract JSON array from the text
        m = re.search(r'\[.*\]', str(raw), re.DOTALL)
        subtopics = json.loads(m.group(0)) if m else []

    if not isinstance(subtopics, list) or len(subtopics) == 0:
        raise ValueError(f"Researcher returned invalid subtopics: {str(raw)[:200]}")

    logger.info(f"Phase 1 done: {len(subtopics)} subtopic(s)")
    return subtopics


async def _process_subtopic(subtopic_data: dict, index: int) -> dict:
    """Phase 2 (per subtopic): run the subtopic pipeline and return result."""
    title = subtopic_data.get("subtopic_title", f"Subtopic {index + 1}")
    logger.info(f"Phase 2 [{index}]: starting '{title}'")

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

        # Inject the single subtopic into session state so the scriptwriter
        # can read it via {SUBTOPIC_DATA?}
        session.state["SUBTOPIC_DATA"] = json.dumps(subtopic_data)

        final_text = ""
        video_url = None

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
            if hasattr(event, "content") and event.content:
                for part in (event.content.parts or []):
                    if hasattr(part, "text") and part.text:
                        final_text = part.text
                    if hasattr(part, "function_response") and part.function_response:
                        resp = part.function_response.response
                        if isinstance(resp, dict) and resp.get("video_url"):
                            video_url = resp["video_url"]

        # Fallback: check session state for video URL
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

        logger.info(f"Phase 2 [{index}]: '{title}' → video_url={video_url}")
        return {
            "subtopic_title": title,
            "video_url": video_url,
            "index": index,
        }

    except Exception as e:
        logger.error(f"Phase 2 [{index}]: '{title}' failed: {e}")
        return {
            "subtopic_title": title,
            "video_url": None,
            "index": index,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/generate")
async def generate(req: GenerateRequest):
    """Run the full agent pipeline: research → parallel subtopic videos."""
    # Phase 1: research
    try:
        subtopics = await _run_researcher(req.topic)
    except Exception as e:
        logger.error(f"Research phase failed: {e}")
        return {"status": "error", "videos": [], "error": str(e)}

    # Phase 2: process all subtopics in parallel
    tasks = [_process_subtopic(st, i) for i, st in enumerate(subtopics)]
    results = await asyncio.gather(*tasks)

    has_video = any(r.get("video_url") for r in results)
    logger.info(f"Pipeline done: {sum(1 for r in results if r.get('video_url'))}/{len(results)} videos")

    return {
        "status": "success" if has_video else "all_failed",
        "videos": sorted(results, key=lambda r: r["index"]),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "agents": ["researcher", "subtopic_pipeline"]}
