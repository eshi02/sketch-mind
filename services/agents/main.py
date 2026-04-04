"""Agent service: wraps ADK orchestrator as a REST API."""
import os, re, logging
from fastapi import FastAPI
from pydantic import BaseModel
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import root_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sketchmind-agents")

app = FastAPI(title="SketchMind Agents")
session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name="sketchmind",
                session_service=session_service)


class GenerateRequest(BaseModel):
    topic: str
    session_id: str = "default"


@app.post("/generate")
async def generate(req: GenerateRequest):
    """Run the full agent pipeline for a topic. Returns video_url."""
    session = await session_service.create_session(
        app_name="sketchmind", user_id="user")

    final_text = ""
    video_url = None
    logger.info(f"Starting pipeline for topic: {req.topic}")

    async for event in runner.run_async(
        user_id="user",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(text=f"Create an educational video explaining: {req.topic}")],
        ),
    ):
        # Log every event
        author = getattr(event, "author", "unknown")
        logger.info(f"Event from '{author}': {type(event).__name__}")

        if hasattr(event, "content") and event.content:
            for part in (event.content.parts or []):
                if hasattr(part, "text") and part.text:
                    final_text = part.text
                    logger.info(f"  Text from '{author}': {part.text[:200]}")
                if hasattr(part, "function_call") and part.function_call:
                    logger.info(f"  Function call: {part.function_call.name}")
                if hasattr(part, "function_response") and part.function_response:
                    resp = part.function_response.response
                    logger.info(f"  Function response: {resp}")
                    if isinstance(resp, dict) and resp.get("video_url"):
                        video_url = resp["video_url"]

    # Try getting video_url from session state (set by renderer agent)
    session_state = (await session_service.get_session(
        app_name="sketchmind", user_id="user", session_id=session.id
    )).state
    logger.info(f"Session state keys: {list(session_state.keys())}")

    if not video_url:
        # Check all state keys for a video URL
        for key in ["RENDER_ERROR", "RENDER_RESULT"]:
            val = session_state.get(key, "")
            if val:
                logger.info(f"{key} from state: {str(val)[:500]}")
                match = re.search(r'https://storage\.googleapis\.com/\S+\.mp4', str(val))
                if match:
                    video_url = match.group(0)
                    break
        # Fallback: search final text
        if not video_url and final_text:
            match = re.search(r'https://storage\.googleapis\.com/\S+\.mp4', final_text)
            video_url = match.group(0) if match else None

    logger.info(f"Pipeline done. video_url={video_url}")

    return {
        "status": "success" if video_url else "no_video",
        "video_url": video_url,
        "agent_response": final_text[:2000],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "agent": root_agent.name}
