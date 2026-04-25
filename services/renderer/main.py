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


def _calc_speaking_rate(audio_script: str, target_duration: float) -> float:
    """Calculate TTS speaking_rate so narration fits the video's target_duration.

    Uses a words-per-second heuristic calibrated against Google TTS Journey
    voice at the base rate of 0.95.  The returned rate is clamped to
    Google TTS limits [0.25, 4.0].
    """
    BASE_RATE = 0.95
    WPS_AT_BASE = 2.5  # words/sec at speaking_rate=0.95 (empirically measured)
    word_count = len(audio_script.split())
    baseline_duration = word_count / WPS_AT_BASE  # estimated duration at base rate
    if target_duration <= 0 or baseline_duration <= 0:
        return BASE_RATE
    ideal_rate = BASE_RATE * (baseline_duration / target_duration)
    # Cap at 1.0× so the voice stays at natural speed.
    # If the narration is too long for the video, the merge step's
    # fallback (setpts / tpad) handles the remainder.
    clamped = max(0.7, min(1.0, ideal_rate))
    logger.info(
        "Calculated speaking_rate=%.2f for target_duration=%.1fs "
        "(words=%d, baseline=%.1fs, ideal=%.2f)",
        clamped, target_duration, word_count, baseline_duration, ideal_rate,
    )
    return clamped


def _synthesize_speech(text: str, output_path: str,
                       speaking_rate: float = 0.95) -> bool:
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
            speaking_rate=speaking_rate,
        )
        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config,
        )
        with open(output_path, "wb") as f:
            f.write(response.audio_content)
        logger.info("TTS synthesis complete: %s (rate=%.2f)", output_path, speaking_rate)
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
    """Merge video and audio by stretching video to match audio duration.

    The video is retimed via setpts so it plays for exactly the same duration
    as the audio track, keeping narration and visuals in sync.
    """
    try:
        video_dur = _get_duration(video_path)
        audio_dur = _get_duration(audio_path)
        if video_dur is None or audio_dur is None or video_dur == 0:
            return False

        ratio = audio_dur / video_dur
        logger.info("Duration sync: video=%.1fs, audio=%.1fs, ratio=%.3f",
                     video_dur, audio_dur, ratio)

        if 0.95 <= ratio <= 1.05:
            # Durations already match — straight copy, no re-encoding
            cmd = [
                "ffmpeg", "-i", video_path, "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac",
                "-map", "0:v", "-map", "1:a",
                "-y", output_path,
            ]
        else:
            # Stretch/compress video to match audio duration
            logger.info("Retiming video by %.2fx to match audio", ratio)
            cmd = [
                "ffmpeg", "-i", video_path, "-i", audio_path,
                "-filter:v", f"setpts=PTS*{ratio}",
                "-c:a", "aac", "-map", "0:v", "-map", "1:a",
                "-y", output_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error("ffmpeg merge failed: %s", result.stderr[-500:])
            return False
        return True
    except Exception as e:
        logger.error("Audio-video merge failed: %s", e)
        return False


def _tts_step(rid: str, work_dir: str, audio_script: str,
              speaking_rate: float = 0.95) -> str | None:
    """Synthesize narration at the given rate. Returns mp3 path on success, None on failure."""
    t1 = time.time()
    audio_path = os.path.join(work_dir, "narration.mp3")
    ok = _synthesize_speech(audio_script, audio_path, speaking_rate=speaking_rate)
    logger.info("[%s] TTS: %.1fs (rate=%.2f, %s)", rid, time.time() - t1,
                speaking_rate, "ok" if ok else "failed")
    return audio_path if ok else None


def _manim_step(rid: str, work_dir: str, python_code: str,
                scene_class_name: str, quality: str) -> tuple[str | None, str | None]:
    """Render the Manim scene. Returns (silent_video_path, error). On success, error is None."""
    t2 = time.time()
    scene_file = os.path.join(work_dir, "scene.py")
    with open(scene_file, "w") as f:
        f.write(python_code)

    try:
        result = subprocess.run(
            ["python3", "-m", "manim", "render", f"-q{quality}",
             "--media_dir", work_dir, scene_file, scene_class_name],
            capture_output=True, text=True, timeout=240, cwd=work_dir,
        )
    except subprocess.TimeoutExpired:
        logger.info("[%s] Manim render: TIMEOUT after >240s", rid)
        return None, "Render timed out (>4 min)"

    logger.info("[%s] Manim render: %.1fs (exit=%d)", rid, time.time() - t2, result.returncode)
    if result.returncode != 0:
        return None, result.stderr[-1500:]

    qmap = {"l": "480p15", "m": "720p30", "h": "1080p60"}
    vdir = os.path.join(work_dir, "videos", "scene", qmap.get(quality, "720p30"))
    try:
        vfiles = [f for f in os.listdir(vdir) if f.endswith(".mp4")]
    except FileNotFoundError:
        return None, "No .mp4 produced"
    if not vfiles:
        return None, "No .mp4 produced"
    return os.path.join(vdir, vfiles[0]), None


@app.post("/render")
async def render_video(req: RenderRequest):
    """Video-first orchestrator: render Manim → measure duration → TTS at
    adjusted rate → merge.

    By rendering the video first and adjusting the TTS speaking_rate to match
    the video's actual duration, the narration and animation stay in sync
    without post-hoc stretching or freezing.
    """
    rid = uuid.uuid4().hex[:8]
    work_dir = tempfile.mkdtemp(prefix=f"m_{rid}_")
    start = time.time()
    loop = asyncio.get_event_loop()

    try:
        # ── Step 1: Render Manim video ──────────────────────────────────
        silent_video, manim_err = await loop.run_in_executor(
            None, _manim_step, rid, work_dir, req.python_code,
            req.scene_class_name, req.quality,
        )
        if manim_err:
            return {"status": "error", "error": manim_err}

        upload_path = silent_video
        has_audio = False

        # ── Step 2: TTS with rate matched to video duration ─────────────
        if req.audio_script:
            # Measure the rendered video's actual duration
            video_dur = await loop.run_in_executor(None, _get_duration, silent_video)
            if video_dur and video_dur > 0:
                speaking_rate = _calc_speaking_rate(req.audio_script, video_dur)
            else:
                speaking_rate = 0.95  # fallback to default
                logger.warning("[%s] Could not measure video duration, using default TTS rate", rid)

            audio_path = await loop.run_in_executor(
                None, _tts_step, rid, work_dir, req.audio_script, speaking_rate,
            )

            # ── Step 3: Merge ───────────────────────────────────────────
            if audio_path:
                t3 = time.time()
                final_video = os.path.join(work_dir, "final.mp4")
                merge_ok = await loop.run_in_executor(
                    None, _merge_with_sync, silent_video, audio_path, final_video,
                )
                logger.info("[%s] FFmpeg merge: %.1fs (%s)", rid, time.time() - t3,
                            "ok" if merge_ok else "failed")
                if merge_ok:
                    upload_path = final_video
                    has_audio = True
                else:
                    logger.warning("[%s] Audio merge failed, uploading silent video", rid)

        # ── Step 4: Upload to GCS ───────────────────────────────────────
        t4 = time.time()
        def _upload() -> str:
            client = storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
            blob = client.bucket(GCS_BUCKET).blob(
                f"videos/{rid}_{req.scene_class_name}.mp4"
            )
            blob.upload_from_filename(upload_path, content_type="video/mp4")
            blob.make_public()
            return blob.public_url

        public_url = await loop.run_in_executor(None, _upload)
        logger.info("[%s] GCS upload: %.1fs", rid, time.time() - t4)

        total = round(time.time() - start, 1)
        logger.info("[%s] Total render: %.1fs (audio=%s)", rid, total, has_audio)
        return {"status": "success", "video_url": public_url,
                "has_audio": has_audio,
                "render_time": total}

    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        await loop.run_in_executor(
            None, partial(subprocess.run, ["rm", "-rf", work_dir], capture_output=True),
        )


@app.get("/health")
async def health():
    r = subprocess.run(["python3", "-c", "import manim; print(manim.__version__)"],
                       capture_output=True, text=True, timeout=10)
    return {"status": "ok", "manim": r.stdout.strip()}
