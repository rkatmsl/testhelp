import subprocess
import threading
import signal
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI(title="YouTube Live Recorder with Trim")

# Folders
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

app.mount("/downloads", StaticFiles(directory=DOWNLOAD_DIR), name="downloads")

templates = Jinja2Templates(directory="templates")

# Active recordings: video_id → (process, output_path_str)
active_processes = {}

# Path to ffmpeg (change if needed, e.g. r"C:\ffmpeg\bin\ffmpeg.exe" on Windows)
FFMPEG_PATH = "ffmpeg"

def parse_time_to_seconds(time_str: str) -> float | None:
    """Convert 2:30 / 02:30 / 150 → seconds. Returns None if invalid/empty"""
    if not time_str or not time_str.strip():
        return None
    time_str = time_str.strip()
    try:
        if ':' in time_str:
            parts = time_str.split(':')
            if len(parts) == 2:  # mm:ss
                m, s = map(float, parts)
                return m * 60 + s
            elif len(parts) == 3:  # hh:mm:ss
                h, m, s = map(float, parts)
                return h * 3600 + m * 60 + s
        return float(time_str)  # plain seconds
    except:
        return None

def record_live_stream(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_time.strftime("%Y%m%d_%H%M%S")
    output_filename = f"{video_id}_{timestamp}.mp4"
    output_path = DOWNLOAD_DIR / output_filename

    print(f"🎥 Started recording: {url}")
    print(f"📁 Saving to: {output_path}")

    cmd = [
        "yt-dlp",
        "--live-from-start",           # try to get from beginning if possible
        "-f", "bv*+ba/best",
        "-o", str(output_path),
        "--merge-output-format", "mp4",
        url
    ]

    try:
        process = subprocess.Popen(cmd)
        active_processes[video_id] = (process, str(output_path))

        process.wait()

        if video_id in active_processes:
            del active_processes[video_id]
        print(f"✅ Recording completed: {output_path}")

    except Exception as e:
        print(f"❌ Error during recording of {video_id}: {e}")
        if video_id in active_processes:
            del active_processes[video_id]

def clip_video(input_path: str, start_time: str, end_time: str = "", output_path: str = "") -> tuple[bool, str]:
    start_sec = parse_time_to_seconds(start_time)
    if start_sec is None:
        return False, "Invalid start time format"

    cmd = [
        FFMPEG_PATH,
        "-i", input_path,
        "-ss", str(start_sec),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
    ]

    if end_time:
        end_sec = parse_time_to_seconds(end_time)
        if end_sec is None:
            return False, "Invalid end time format"
        if end_sec <= start_sec:
            return False, "End time must be after start time"
        duration = end_sec - start_sec
        cmd.extend(["-t", str(duration)])

    if not output_path:
        base = Path(input_path).stem
        output_path = str(Path(input_path).parent / f"{base}_clip.mp4")
    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True, f"Clip created: {output_path}"
        else:
            return False, f"ffmpeg failed: {result.stderr.strip() or 'unknown error'}"
    except Exception as e:
        return False, f"Failed to run ffmpeg: {str(e)}"

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    recordings = []
    for file in DOWNLOAD_DIR.glob("*.mp4"):
        filename = file.name
        parts = filename.split("_", 1)
        video_id = parts[0] if len(parts) > 1 else "unknown"
        size_mb = round(file.stat().st_size / (1024 * 1024), 2) if file.stat().st_size > 0 else 0
        recordings.append({
            "filename": filename,
            "video_id": video_id,
            "size_mb": size_mb,
            "url": f"/downloads/{filename}"
        })

    active = []
    for vid, (proc, out) in active_processes.items():
        active.append({
            "video_id": vid,
            "output": os.path.basename(out),
            "pid": proc.pid
        })

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "active_recordings": active,
            "completed_recordings": sorted(recordings, key=lambda x: x["filename"], reverse=True)
        }
    )

@app.post("/start-recording")
async def start_recording(video_id: str = Form(...)):
    video_id = video_id.strip()
    if not video_id:
        raise HTTPException(400, "Video ID is required")

    if video_id in active_processes:
        return RedirectResponse(url="/", status_code=303)

    thread = threading.Thread(target=record_live_stream, args=(video_id,), daemon=True)
    thread.start()

    return RedirectResponse(url="/", status_code=303)

@app.post("/stop-recording")
async def stop_recording(video_id: str = Form(...)):
    if video_id not in active_processes:
        raise HTTPException(400, "No active recording for this video ID")

    process, output_path = active_processes[video_id]
    print(f"🛑 Stopping {video_id} (PID: {process.pid})")
    process.send_signal(signal.SIGINT)

    # Redirect to trim prompt (even if process hasn't fully exited yet)
    return RedirectResponse(url=f"/trim-prompt?video_id={video_id}", status_code=303)

@app.get("/trim-prompt", response_class=HTMLResponse)
async def trim_prompt(request: Request, video_id: str):
    output_path = None
    output_filename = ""

    # Try active first
    if video_id in active_processes:
        _, output_path = active_processes[video_id]
        output_filename = os.path.basename(output_path)
    else:
        # Find most recent completed file for this video_id
        candidates = list(DOWNLOAD_DIR.glob(f"{video_id}_*.mp4"))
        if candidates:
            latest = max(candidates, key=lambda p: p.stat().st_mtime)
            output_path = str(latest)
            output_filename = latest.name

    if not output_filename:
        raise HTTPException(404, "No recording found for this video ID")

    return templates.TemplateResponse(
        "trim_prompt.html",
        {
            "request": request,
            "video_id": video_id,
            "output_filename": output_filename,
            "output_path": output_path or ""
        }
    )

@app.post("/perform-trim")
async def perform_trim(
    video_id: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(default=""),
    keep_original: bool = Form(default=True)
):
    candidates = list(DOWNLOAD_DIR.glob(f"{video_id}_*.mp4"))
    if not candidates:
        raise HTTPException(404, "No recording found")

    input_path = max(candidates, key=lambda p: p.stat().st_mtime)
    input_filename = input_path.stem

    clip_filename = f"{input_filename}_clip.mp4"
    clip_path = DOWNLOAD_DIR / clip_filename

    success, message = clip_video(
        str(input_path),
        start_time,
        end_time,
        str(clip_path)
    )

    if not success:
        raise HTTPException(500, message)

    if not keep_original:
        try:
            input_path.unlink()
            print(f"Original file deleted: {input_path}")
        except Exception as e:
            print(f"Could not delete original: {e}")

    return RedirectResponse(url="/", status_code=303)

@app.post("/delete-recording")
async def delete_recording(filename: str = Form(...)):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")
    try:
        file_path.unlink()
        print(f"🗑️ Deleted: {file_path}")
    except Exception as e:
        raise HTTPException(500, f"Failed to delete: {str(e)}")
    return RedirectResponse(url="/", status_code=303)

@app.get("/downloads/{filename}")
async def get_file(filename: str):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(file_path, filename=filename)

@app.get("/check-ffmpeg")
async def check_ffmpeg():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        return {"status": "ok", "version": result.stdout.splitlines()[0]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}