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

app = FastAPI(title="YouTube Live Recorder")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

app.mount("/downloads", StaticFiles(directory=DOWNLOAD_DIR), name="downloads")

templates = Jinja2Templates(directory="templates")

active_processes = {}

FFMPEG_PATH = "ffmpeg"  # should be available on Railway after adding RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg

@app.get("/trim-form", response_class=HTMLResponse)
async def trim_form(request: Request, filename: str):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    return templates.TemplateResponse("trim-form.html", {
        "request": request,
        "filename": filename
    })

def parse_time_to_seconds(time_str: str) -> float | None:
    if not time_str or not time_str.strip():
        return None
    time_str = time_str.strip()
    try:
        if ':' in time_str:
            parts = time_str.split(':')
            if len(parts) == 2:
                m, s = map(float, parts)
                return m * 60 + s
            elif len(parts) == 3:
                h, m, s = map(float, parts)
                return h * 3600 + m * 60 + s
        return float(time_str)
    except:
        return None

def record_live_stream(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_time.strftime("%Y%m%d_%H%M%S")
    output_filename = f"{video_id}_{timestamp}.mp4"
    output_path = DOWNLOAD_DIR / output_filename

    print(f"🎥 Started: {url} → {output_path}")

    cmd = [
        "yt-dlp",
        "--live-from-start",
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
        print(f"✅ Done: {output_path}")
    except Exception as e:
        print(f"❌ Error: {e}")
        if video_id in active_processes:
            del active_processes[video_id]

def create_clip(input_path: str, start_time: str, end_time: str = "") -> tuple[bool, str]:
    start_sec = parse_time_to_seconds(start_time)
    if start_sec is None:
        return False, "Invalid start time"

    input_path_obj = Path(input_path)
    video_id = input_path_obj.stem.split('_')[0]
    start_str = start_time.replace(':', '').replace(' ', '')
    end_str = end_time.replace(':', '').replace(' ', '') if end_time else 'end'
    clip_filename = f"{video_id}_{start_str}-{end_str}.mp4"
    clip_path = input_path_obj.parent / clip_filename

    cmd = [
        FFMPEG_PATH,
        "-i", input_path,
        "-ss", str(start_sec),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
    ]

    if end_time:
        end_sec = parse_time_to_seconds(end_time)
        if end_sec is None or end_sec <= start_sec:
            return False, "Invalid end time or end <= start"
        duration = end_sec - start_sec
        cmd.extend(["-t", str(duration)])

    cmd.append(str(clip_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and clip_path.exists() and clip_path.stat().st_size > 1000:
            return True, f"Clip saved as: {clip_filename}"
        else:
            return False, f"ffmpeg error: {result.stderr.strip() or 'unknown'}"
    except Exception as e:
        return False, f"Failed to run ffmpeg: {str(e)}"

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    recordings = []
    for file in DOWNLOAD_DIR.glob("*.mp4"):
        filename = file.name
        parts = filename.split("_", 1)
        video_id = parts[0] if len(parts) > 1 else "unknown"
        size_mb = round(file.stat().st_size / (1024**2), 2) if file.stat().st_size > 0 else 0
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
        raise HTTPException(400, "Video ID required")

    if video_id in active_processes:
        return RedirectResponse(url="/", status_code=303)

    thread = threading.Thread(target=record_live_stream, args=(video_id,), daemon=True)
    thread.start()

    return RedirectResponse(url="/", status_code=303)

@app.post("/stop-recording")
async def stop_recording(video_id: str = Form(...)):
    if video_id not in active_processes:
        raise HTTPException(400, "No active recording")

    process, _ = active_processes[video_id]
    process.send_signal(signal.SIGINT)

    return RedirectResponse(url="/", status_code=303)

@app.post("/trim-recording")
async def trim_recording(
    filename: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(default="")
):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")

    success, message = create_clip(str(file_path), start_time, end_time)

    if not success:
        raise HTTPException(500, message)

    return RedirectResponse(url="/", status_code=303)

@app.post("/delete-recording")
async def delete_recording(filename: str = Form(...)):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    file_path.unlink()
    return RedirectResponse(url="/", status_code=303)

@app.get("/downloads/{filename}")
async def get_file(filename: str):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(file_path, filename=filename)