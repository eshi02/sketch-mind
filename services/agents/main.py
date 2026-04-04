"""Agent service: wraps ADK orchestrator as a REST API."""
import os, re
from fastapi import FastAPI
from pydantic import BaseModel
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from agent import root_agent

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
    async for event in runner.run_async(
        user_id="user",
        session_id=session.id,
        new_message=f"Create an educational video explaining: {req.topic}",
    ):
        if hasattr(event, "content") and event.content:
            for part in (event.content.parts or []):
                if hasattr(part, "text") and part.text:
                    final_text = part.text

    # Extract video URL
    match = re.search(r'https://storage\.googleapis\.com/\S+\.mp4', final_text)
    video_url = match.group(0) if match else None

    return {
        "status": "success" if video_url else "no_video",
        "video_url": video_url,
        "agent_response": final_text[:2000],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "agent": root_agent.name}
