import os, subprocess, tempfile, uuid, time
from fastapi import FastAPI
from pydantic import BaseModel
from google.cloud import storage

app = FastAPI(title="SketchMind Renderer")
GCS_BUCKET = os.getenv("GCS_BUCKET")


class RenderRequest(BaseModel):
    python_code: str
    scene_class_name: str = "GeneratedScene"
    quality: str = "l"


@app.post("/render")
async def render_video(req: RenderRequest):
    start = time.time()
    rid = uuid.uuid4().hex[:8]
    work_dir = tempfile.mkdtemp(prefix=f"m_{rid}_")

    try:
        scene_file = os.path.join(work_dir, "scene.py")
        with open(scene_file, "w") as f:
            f.write(req.python_code)

        result = subprocess.run(
            ["python3", "-m", "manim", "render", f"-q{req.quality}",
             "--media_dir", work_dir, scene_file, req.scene_class_name],
            capture_output=True, text=True, timeout=240, cwd=work_dir,
        )

        if result.returncode != 0:
            return {"status": "error", "error": result.stderr[-1500:]}

        qmap = {"l": "480p15", "m": "720p30", "h": "1080p60"}
        vdir = os.path.join(work_dir, "videos", "scene", qmap.get(req.quality, "720p30"))
        vfiles = [f for f in os.listdir(vdir) if f.endswith(".mp4")]
        if not vfiles:
            return {"status": "error", "error": "No .mp4 produced"}

        # Upload to GCS
        client = storage.Client()
        blob = client.bucket(GCS_BUCKET).blob(f"videos/{rid}_{req.scene_class_name}.mp4")
        blob.upload_from_filename(os.path.join(vdir, vfiles[0]), content_type="video/mp4")
        blob.make_public()

        return {"status": "success", "video_url": blob.public_url,
                "render_time": round(time.time() - start, 1)}

    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Render timed out (>4 min)"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        subprocess.run(["rm", "-rf", work_dir], capture_output=True)


@app.get("/health")
async def health():
    r = subprocess.run(["python3", "-c", "import manim; print(manim.__version__)"],
                       capture_output=True, text=True, timeout=10)
    return {"status": "ok", "manim": r.stdout.strip()}
