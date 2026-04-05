import os, httpx
import google.auth.transport.requests
import google.oauth2.id_token

RENDER_URL = os.getenv("RENDER_SERVICE_URL")


def _get_auth_token(audience: str) -> str:
    """Get ID token for Cloud Run service-to-service auth."""
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, audience)


def render_manim_video(python_code: str, audio_script: str = "") -> dict:
    """Sends Manim code to the renderer service and returns the video URL.

    Args:
        python_code: Complete Manim Python script with a class named GeneratedScene.
        audio_script: Full narration text to synthesize as voiceover audio.

    Returns:
        dict with status and video_url or error.
    """
    scene_class_name = "GeneratedScene"
    quality = "l"
    try:
        headers = {}
        if RENDER_URL and "run.app" in RENDER_URL:
            token = _get_auth_token(RENDER_URL)
            headers["Authorization"] = f"Bearer {token}"

        body = {"python_code": python_code,
                "scene_class_name": scene_class_name,
                "quality": quality}
        if audio_script:
            body["audio_script"] = audio_script

        resp = httpx.post(
            f"{RENDER_URL}/render",
            json=body,
            headers=headers,
            timeout=300,
        )
        return resp.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}
