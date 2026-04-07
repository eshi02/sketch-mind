import os, subprocess, tempfile, uuid, time, logging, re, asyncio
from functools import partial
from fastapi import FastAPI
from pydantic import BaseModel
from google.cloud import storage
from google.cloud import texttospeech

app = FastAPI(title="SketchMind Renderer")
GCS_BUCKET = os.getenv("GCS_BUCKET")
logger = logging.getLogger(__name__)

_tts_client = None


def _get_tts_client():
    global _tts_client
    if _tts_client is None:
        _tts_client = texttospeech.TextToSpeechClient()
    return _tts_client


class RenderRequest(BaseModel):
    python_code: str
    scene_class_name: str = "GeneratedScene"
    quality: str = "l"
    audio_script: str | None = None


def _clean_audio_text(text: str) -> str:
    """Strip markup, special characters, and formatting that TTS would read literally."""
    # Remove markdown-style formatting: *bold*, **bold**, _italic_, etc.
    text = re.sub(r'[*_~`#]', '', text)
    # Remove content in parentheses/brackets (often technical asides)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    # Replace math/code symbols with spoken equivalents
    text = text.replace('²', ' squared')
    text = text.replace('³', ' cubed')
    text = text.replace('≈', ' approximately equals ')
    text = text.replace('≠', ' does not equal ')
    text = text.replace('≤', ' less than or equal to ')
    text = text.replace('≥', ' greater than or equal to ')
    text = text.replace(' = ', ' equals ')
    text = text.replace(' + ', ' plus ')
    text = text.replace(' - ', ' minus ')
    text = text.replace(' / ', ' divided by ')
    text = text.replace('→', ' leads to ')
    text = text.replace('&', ' and ')
    # Remove any remaining non-speech characters
    text = re.sub(r'[{}<>|\\^]', '', text)
    # Collapse multiple spaces/newlines
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _synthesize_speech(text: str, output_path: str) -> bool:
    """Convert text to speech using Google Cloud TTS Journey voice. Returns True on success."""
    try:
        client = _get_tts_client()
        cleaned = _clean_audio_text(text)
        logger.info("TTS input cleaned: %d chars → %d chars", len(text), len(cleaned))

        synthesis_input = texttospeech.SynthesisInput(text=cleaned)
        # Journey voices are Google's most natural, human-like voices
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name="en-US-Journey-D",
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.95,  # slightly slower for educational clarity
        )
        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config,
        )
        with open(output_path, "wb") as f:
            f.write(response.audio_content)
        logger.info("TTS synthesis complete: %s", output_path)
        return True
    except Exception as e:
        logger.error("TTS synthesis failed: %s", e)
        return False


def _get_duration(file_path: str) -> float | None:
    """Get media duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception as e:
        logger.error("ffprobe failed for %s: %s", file_path, e)
        return None


def _merge_with_sync(video_path: str, audio_path: str, output_path: str) -> bool:
    """Merge video and audio, scaling video speed to match audio duration."""
    try:
        video_dur = _get_duration(video_path)
        audio_dur = _get_duration(audio_path)
        if video_dur is None or audio_dur is None or video_dur == 0:
            return False

        ratio = audio_dur / video_dur
        logger.info("Duration sync: video=%.1fs, audio=%.1fs, ratio=%.3f",
                     video_dur, audio_dur, ratio)

        if 0.8 <= ratio <= 1.2:
            # Scale video tempo to match audio duration
            cmd = [
                "ffmpeg", "-i", video_path, "-i", audio_path,
                "-filter:v", f"setpts=PTS*{ratio}",
                "-c:a", "aac", "-map", "0:v", "-map", "1:a",
                "-y", output_path,
            ]
        else:
            # Durations too different — use shortest to avoid bad pacing
            logger.warning("Duration ratio %.2f outside 0.8-1.2 range, using -shortest", ratio)
            cmd = [
                "ffmpeg", "-i", video_path, "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac",
                "-shortest", "-y", output_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error("ffmpeg merge failed: %s", result.stderr[-500:])
            return False
        return True
    except Exception as e:
        logger.error("Audio-video merge failed: %s", e)
        return False


def _render_sync(req_python_code: str, req_scene_class_name: str,
                  req_quality: str, req_audio_script: str | None) -> dict:
    """All blocking work runs here — called from a thread pool."""
    start = time.time()
    rid = uuid.uuid4().hex[:8]
    work_dir = tempfile.mkdtemp(prefix=f"m_{rid}_")

    try:
        # Step 1: If audio script provided, synthesize speech first
        audio_path = None
        if req_audio_script:
            t1 = time.time()
            audio_path = os.path.join(work_dir, "narration.mp3")
            tts_ok = _synthesize_speech(req_audio_script, audio_path)
            logger.info("[%s] TTS: %.1fs (%s)", rid, time.time() - t1, "ok" if tts_ok else "failed")
            if not tts_ok:
                audio_path = None  # fall back to silent video

        # Step 2: Render Manim video
        t2 = time.time()
        scene_file = os.path.join(work_dir, "scene.py")
        with open(scene_file, "w") as f:
            f.write(req_python_code)

        result = subprocess.run(
            ["python3", "-m", "manim", "render", f"-q{req_quality}",
             "--media_dir", work_dir, scene_file, req_scene_class_name],
            capture_output=True, text=True, timeout=240, cwd=work_dir,
        )
        logger.info("[%s] Manim render: %.1fs (exit=%d)", rid, time.time() - t2, result.returncode)

        if result.returncode != 0:
            return {"status": "error", "error": result.stderr[-1500:]}

        qmap = {"l": "480p15", "m": "720p30", "h": "1080p60"}
        vdir = os.path.join(work_dir, "videos", "scene", qmap.get(req_quality, "720p30"))
        vfiles = [f for f in os.listdir(vdir) if f.endswith(".mp4")]
        if not vfiles:
            return {"status": "error", "error": "No .mp4 produced"}

        silent_video = os.path.join(vdir, vfiles[0])
        upload_path = silent_video
        has_audio = False

        # Step 3: Merge audio with video if TTS succeeded
        if audio_path:
            t3 = time.time()
            final_video = os.path.join(work_dir, "final.mp4")
            merge_ok = _merge_with_sync(silent_video, audio_path, final_video)
            logger.info("[%s] FFmpeg merge: %.1fs (%s)", rid, time.time() - t3, "ok" if merge_ok else "failed")
            if merge_ok:
                upload_path = final_video
                has_audio = True
            else:
                logger.warning("Audio merge failed, uploading silent video")

        # Step 4: Upload to GCS
        t4 = time.time()
        client = storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
        blob = client.bucket(GCS_BUCKET).blob(f"videos/{rid}_{req_scene_class_name}.mp4")
        blob.upload_from_filename(upload_path, content_type="video/mp4")
        blob.make_public()
        logger.info("[%s] GCS upload: %.1fs", rid, time.time() - t4)

        total = round(time.time() - start, 1)
        logger.info("[%s] Total render: %.1fs (audio=%s)", rid, total, has_audio)
        return {"status": "success", "video_url": blob.public_url,
                "has_audio": has_audio,
                "render_time": total}

    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Render timed out (>4 min)"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        subprocess.run(["rm", "-rf", work_dir], capture_output=True)


@app.post("/render")
async def render_video(req: RenderRequest):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        partial(_render_sync, req.python_code, req.scene_class_name,
                req.quality, req.audio_script),
    )


@app.get("/health")
async def health():
    r = subprocess.run(["python3", "-c", "import manim; print(manim.__version__)"],
                       capture_output=True, text=True, timeout=10)
    return {"status": "ok", "manim": r.stdout.strip()}
