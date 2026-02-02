import asyncio
import logging
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import Settings, get_settings, save_config, load_saved_config, is_configured
from core.spotify import SpotifyClient
from core.slskd import SlskdClient
from core.tagger import Tagger
from core.downloader import DownloadOrchestrator

logger = logging.getLogger(__name__)

orchestrator: DownloadOrchestrator | None = None
current_settings: Settings | None = None


def init_orchestrator(settings: Settings) -> DownloadOrchestrator:
    """Create a new orchestrator with the given settings."""
    spotify = SpotifyClient(settings.spotify_client_id, settings.spotify_client_secret)
    slskd = SlskdClient(settings.slskd_host, settings.slskd_api_key)
    tagger = Tagger()
    return DownloadOrchestrator(spotify, slskd, tagger, settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator, current_settings
    settings = get_settings()
    current_settings = settings
    if is_configured(settings):
        orchestrator = init_orchestrator(settings)
        logger.info("Orchestrator initialized with existing config")
    else:
        logger.info("No config found, waiting for setup via /settings")
    yield
    if orchestrator:
        await orchestrator.slskd.close()


app = FastAPI(title="Spotify Downloader", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


class PlaylistRequest(BaseModel):
    url: str


class ConfigRequest(BaseModel):
    spotify_client_id: str
    spotify_client_secret: str
    slskd_api_key: str
    slskd_host: str = ""


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/settings")
async def settings_page():
    return FileResponse("static/settings.html")


@app.get("/api/config")
async def get_config():
    """Get current config (secrets masked)."""
    saved = load_saved_config()
    settings = get_settings()
    return {
        "configured": is_configured(settings),
        "spotify_client_id": settings.spotify_client_id,
        "spotify_client_secret": _mask(settings.spotify_client_secret),
        "slskd_api_key": _mask(settings.slskd_api_key),
        "slskd_host": settings.slskd_host,
    }


@app.post("/api/config")
async def save_config_endpoint(body: ConfigRequest):
    """Save config and reinitialize the orchestrator."""
    global orchestrator, current_settings

    # Close existing slskd client if any
    if orchestrator:
        await orchestrator.slskd.close()

    # Build new settings
    config_data = {
        "spotify_client_id": body.spotify_client_id,
        "spotify_client_secret": body.spotify_client_secret,
        "slskd_api_key": body.slskd_api_key,
    }
    if body.slskd_host:
        config_data["slskd_host"] = body.slskd_host

    save_config(config_data)
    logger.info("Config saved")

    settings = get_settings()
    current_settings = settings

    if not is_configured(settings):
        raise HTTPException(400, "Missing required config fields")

    orchestrator = init_orchestrator(settings)
    logger.info("Orchestrator reinitialized with new config")

    return {"status": "ok", "configured": True}


@app.post("/api/playlist")
async def start_playlist(body: PlaylistRequest, background_tasks: BackgroundTasks):
    if orchestrator is None:
        raise HTTPException(400, "Not configured yet. Go to /settings first.")
    try:
        job = orchestrator.create_job(body.url)
    except Exception as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(orchestrator.process_job, job)
    return {
        "job_id": job.job_id,
        "playlist_name": job.playlist_name,
        "track_count": len(job.tracks),
    }


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    if orchestrator is None:
        raise HTTPException(400, "Not configured yet")
    job = orchestrator.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.model_dump()


@app.get("/api/jobs")
async def list_jobs():
    if orchestrator is None:
        return []
    return [
        {
            "job_id": j.job_id,
            "playlist_name": j.playlist_name,
            "status": j.status,
            "track_count": len(j.tracks),
            "completed": sum(1 for t in j.tracks if t.status == "complete"),
            "failed": sum(1 for t in j.tracks if t.status in ("failed", "not_found")),
        }
        for j in orchestrator.jobs.values()
    ]


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    if orchestrator is None:
        raise HTTPException(400, "Not configured yet")
    if not orchestrator.stop_job(job_id):
        raise HTTPException(400, "Job not running or not found")
    return {"status": "stopping"}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, background_tasks: BackgroundTasks):
    if orchestrator is None:
        raise HTTPException(400, "Not configured yet")
    job = orchestrator.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "stopped":
        raise HTTPException(400, "Job is not stopped")
    orchestrator.resume_job(job)
    background_tasks.add_task(orchestrator.process_job, job)
    return {"status": "resumed"}


@app.websocket("/ws/jobs/{job_id}")
async def ws_job_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        while True:
            if orchestrator is None:
                await websocket.close()
                return
            job = orchestrator.jobs.get(job_id)
            if not job:
                await websocket.send_json({"error": "Job not found"})
                await websocket.close()
                return
            await websocket.send_json(job.model_dump())
            if job.status in ("complete", "stopped"):
                await asyncio.sleep(0.5)
                await websocket.send_json(job.model_dump())
                await websocket.close()
                return
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.get("/api/health")
async def health():
    if orchestrator is None:
        return {"status": "not_configured", "slskd_connected": False}
    try:
        resp = await orchestrator.slskd.client.get("/application")
        slskd_ok = resp.status_code == 200
    except Exception:
        slskd_ok = False
    return {"status": "ok" if slskd_ok else "degraded", "slskd_connected": slskd_ok}


def _mask(value: str) -> str:
    """Mask a secret string, showing only first/last 3 chars."""
    if not value or len(value) < 8:
        return "***" if value else ""
    return f"{value[:3]}...{value[-3:]}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=1337, reload=True)
