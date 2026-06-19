import asyncio
import base64
import json
import logging
import os
import time
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator
from starlette.middleware.cors import CORSMiddleware
from uvicorn.logging import DefaultFormatter

from jvcapture.capture_utils import is_amazon_url, ProxyError
from jvcapture.capture_amazon import capture_amazon
from jvcapture.capture_default import capture_default
from jvcapture.capture_llm import process_image, LLMError
from jvcapture.settings import get_settings
from jvcapture.queue_manager import (
    init_queue_manager,
    get_queue_manager,
    STATUS_QUEUED,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_WEBHOOK_FAILED,
    PRIORITY_NORMAL,
    PRIORITY_EMERGENCY,
)
from jvcapture.job_queue import (
    enqueue_job,
    get_job,
    pop_job_record,
    remove_completed_artifact,
    list_jobs_filtered,
    parse_job_uuid,
    start_worker,
    stop_worker,
    build_artifact_url,
    acquire_slot_for_retried_job,
)

logger = logging.getLogger(__name__)


def _configure_logging_for_uvicorn() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            DefaultFormatter(
                fmt="%(levelprefix)s %(name)s: %(message)s",
                use_colors=sys.stderr.isatty(),
            )
        )
        root.addHandler(handler)


_cleanup_task: asyncio.Task | None = None

_SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"

_SCREENSHOTS_MAX_AGE_SEC = 86400


def _sweep_stale_screenshots() -> int:
    if not _SCREENSHOTS_DIR.is_dir():
        return 0
    now = time.time()
    removed = 0
    for p in _SCREENSHOTS_DIR.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > _SCREENSHOTS_MAX_AGE_SEC:
                p.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


async def cleanup_loop():
    while True:
        try:
            await asyncio.sleep(3600)
            queue_manager = get_queue_manager()
            completed_count = queue_manager.cleanup_completed_jobs()
            failed_count = queue_manager.cleanup_webhook_failed_jobs()
            screenshots_removed = _sweep_stale_screenshots()
            if completed_count > 0 or failed_count > 0 or screenshots_removed > 0:
                logger.info(
                    "Cleanup: %d completed jobs, %d failed jobs removed, %d stale screenshots",
                    completed_count,
                    failed_count,
                    screenshots_removed,
                )
        except Exception as e:
            logger.exception("Error in cleanup loop: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging_for_uvicorn()
    settings = get_settings()
    Path(settings.artifact_dir).mkdir(parents=True, exist_ok=True)
    (Path(settings.artifact_dir) / "spool").mkdir(parents=True, exist_ok=True)

    init_queue_manager(
        artifact_dir=str(settings.artifact_dir),
        completed_retention_hours=settings.completed_job_retention_hours,
        failed_retention_days=settings.failed_webhook_retention_days,
    )

    start_worker(settings)

    global _cleanup_task
    _cleanup_task = asyncio.create_task(cleanup_loop(), name="jvcapture_cleanup")

    yield

    await stop_worker()
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="jvcapture",
    version="3.0.0",
    description=(
        "Screenshot capture and image processing API with anti-detection stealth browsing.\n\n"
        "**POST /capture** — Capture a screenshot and return the base64 PNG image.\n\n"
        "**POST /process** — Capture a screenshot, send it to an LLM with a prompt, "
        "and return the LLM's text response along with token usage.\n\n"
        "**POST /v1/jobs** — Enqueue an async capture/process job with callback URL.\n\n"
        "**GET /v1/jobs** — List all jobs.\n\n"
        "**GET /v1/jobs/{job_id}** — Get job status.\n\n"
        "**GET /v1/artifacts/{job_id}** — Download completed artifact JSON."
    ),
    lifespan=lifespan,
)

_cors_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_settings.resolved_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Shared capture kwargs ──────────────────────────────────────────────

def _build_capture_kwargs(req) -> dict:
    kwargs = dict(
        url=str(req.url),
        max_scrolls=req.max_scrolls,
        headless=True,
        timezone=getattr(req, "timezone", None),
        locale=getattr(req, "locale", None),
        proxy=getattr(req, "proxy", None),
        proxy_timeout=getattr(req, "proxy_timeout", 20),
        geoip=getattr(req, "geoip", None),
        save_image=getattr(req, "save_image", False),
    )
    zip_code = getattr(req, "zip_code", None)
    if zip_code:
        kwargs["zip_code"] = zip_code
    return kwargs


# ── POST /capture ─────────────────────────────────────────────────────

class CaptureRequest(BaseModel):
    url: HttpUrl = Field(
        ...,
        description="The URL of the page to capture as a screenshot.",
    )
    max_scrolls: int = Field(
        default=30,
        ge=1,
        le=200,
        description=(
            "Maximum number of scroll-and-screenshot steps. "
            "Increase for very long pages, decrease for faster captures."
        ),
    )

    @field_validator("max_scrolls", mode="before")
    @classmethod
    def coerce_max_scrolls(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                raise ValueError(f"max_scrolls must be an integer, got '{v}'")
        return v

    timezone: Optional[str] = Field(
        default=None,
        description="IANA timezone for the browser session (e.g. 'America/New_York').",
    )
    locale: Optional[str] = Field(
        default=None,
        description="BCP 47 locale for the browser session (e.g. 'en-US').",
    )
    proxy: Optional[str] = Field(
        default=None,
        description="Proxy URL (e.g. 'http://user:pass@proxy:8080').",
    )
    proxy_timeout: int = Field(
        default=20,
        ge=5,
        le=60,
        description="Timeout in seconds for proxy validation.",
    )
    geoip: Optional[bool] = Field(
        default=None,
        description="Auto-detect timezone/locale from proxy IP.",
    )
    save_image: bool = Field(
        default=False,
        description="Save screenshot PNG to local 'screenshots/' directory.",
    )
    zip_code: Optional[str] = Field(
        default=None,
        description="ZIP code for Amazon location-based pricing.",
    )


class CaptureResponse(BaseModel):
    site: str = Field(..., description='Detected site type ("amazon" or "unknown").')
    url: str = Field(..., description="The URL that was captured.")
    image: str = Field(..., description="Base64-encoded PNG screenshot of the page.")
    image_path: Optional[str] = Field(
        default=None,
        description="Local file path of the saved screenshot (only when save_image=true).",
    )


@app.post(
    "/capture",
    response_model=CaptureResponse,
    summary="Capture a screenshot of a URL",
    description=(
        "Navigate to the given URL in a stealth browser, scroll through the page, "
        "and return a stitched full-page screenshot as base64 PNG."
    ),
)
def capture(req: CaptureRequest):
    url = str(req.url)
    kwargs = _build_capture_kwargs(req)

    try:
        if is_amazon_url(url):
            result = capture_amazon(**kwargs)
        else:
            result = capture_default(**kwargs)
    except ProxyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Capture failed: {str(e)}")

    image_b64 = base64.b64encode(result["image_bytes"]).decode("utf-8")

    return CaptureResponse(
        site=result["site"],
        url=result["url"],
        image=image_b64,
        image_path=result.get("image_path"),
    )


# ── POST /process ─────────────────────────────────────────────────────

class ProcessRequest(BaseModel):
    url: HttpUrl = Field(
        ...,
        description="The URL of the page to capture and process.",
    )
    prompt: str = Field(
        ...,
        description="The prompt to send to the LLM along with the screenshot.",
    )
    max_scrolls: int = Field(
        default=5,
        ge=1,
        le=200,
        description="Maximum scroll steps. Lower values are faster; 5 is usually enough for product pages.",
    )

    @field_validator("max_scrolls", mode="before")
    @classmethod
    def coerce_max_scrolls(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                raise ValueError(f"max_scrolls must be an integer, got '{v}'")
        return v

    provider: Optional[str] = Field(
        default=None,
        description="LLM provider (openai, anthropic, ollama, groq, openrouter). Defaults to JVCAPTURE_LLM_PROVIDER env var.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Model name override. Defaults to JVCAPTURE_LLM_MODEL env var or provider default.",
    )
    temperature: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Sampling temperature (0.0-2.0). Defaults to JVCAPTURE_LLM_TEMPERATURE env var or 0.7.",
    )
    max_tokens: Optional[int] = Field(
        default=None,
        ge=1,
        le=16384,
        description="Maximum LLM response tokens. Defaults to JVCAPTURE_LLM_MAX_TOKENS env var or 1000.",
    )
    proxy: Optional[str] = Field(
        default=None,
        description="Proxy URL for the browser.",
    )
    zip_code: Optional[str] = Field(
        default=None,
        description="ZIP code for Amazon location-based pricing.",
    )
    save_image: bool = Field(
        default=False,
        description="Save screenshot PNG to local 'screenshots/' directory.",
    )


class ProcessResponse(BaseModel):
    site: str = Field(..., description='Detected site type ("amazon" or "unknown").')
    url: str = Field(..., description="The URL that was captured.")
    description: str = Field(..., description="LLM text response from processing the screenshot with the prompt.")
    tokens_used: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Token usage from the LLM response (provider-specific keys).",
    )
    image_path: Optional[str] = Field(
        default=None,
        description="Local file path of the saved screenshot (only when save_image=true).",
    )


@app.post(
    "/process",
    response_model=ProcessResponse,
    summary="Capture a screenshot and process it with an LLM",
    description=(
        "Capture a screenshot of the URL, send it to a vision-capable LLM with the "
        "given prompt, and return the LLM's text response. LLM configuration "
        "(provider, model, API key) defaults to environment variables and can be "
        "optionally overridden per request."
    ),
)
def process_url(req: ProcessRequest):
    url = str(req.url)
    logger.info("Process request: url=%s prompt_len=%d provider=%s model=%s max_scrolls=%d",
                url, len(req.prompt), req.provider, req.model, req.max_scrolls)
    kwargs = dict(
        url=url,
        max_scrolls=req.max_scrolls,
        headless=True,
        proxy=req.proxy,
        save_image=req.save_image,
    )
    if req.zip_code:
        kwargs["zip_code"] = req.zip_code

    try:
        if is_amazon_url(url):
            result = capture_amazon(**kwargs)
        else:
            result = capture_default(**kwargs)
    except ProxyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Capture failed: {str(e)}")

    image_b64 = base64.b64encode(result["image_bytes"]).decode("utf-8")

    try:
        llm_result = process_image(
            image_b64=image_b64,
            prompt=req.prompt,
            provider=req.provider,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM processing failed: {str(e)}")

    response = ProcessResponse(
        site=result["site"],
        url=result["url"],
        description=llm_result["description"],
        tokens_used=llm_result.get("tokens_used"),
        image_path=result.get("image_path"),
    )
    logger.warning(
        "Process response: url=%s site=%s description_len=%d tokens=%s image_path=%s",
        url,
        result["site"],
        len(llm_result.get("description") or ""),
        llm_result.get("tokens_used"),
        result.get("image_path"),
    )
    return response


# ── GET /health ───────────────────────────────────────────────────────

@app.get(
    "/health",
    summary="Health check",
    description="Returns OK if the service is running and ready to accept requests.",
)
def health():
    return {"status": "ok"}


# ── Async Job Queue Routes (/v1/) ─────────────────────────────────────

@app.post(
    "/v1/jobs",
    summary="Enqueue an async capture/process job",
    description=(
        "Submit a URL for asynchronous capture (and optional LLM processing). "
        "Returns 202 with a job_id. Results are delivered via callback_url webhook "
        "or can be polled via GET /v1/artifacts/{job_id}."
    ),
    status_code=202,
)
async def enqueue_capture_job(request: Request):
    settings = get_settings()
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        body = await request.json()
    elif "multipart/form-data" in content_type:
        form = await request.form()
        body = {}
        for key, value in form.items():
            if key == "file":
                continue
            body[key] = str(value) if not isinstance(value, str) else value
    else:
        body = await request.json()

    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    capture_type = body.get("capture_type", "capture")
    if capture_type not in ("capture", "process"):
        raise HTTPException(status_code=400, detail="capture_type must be 'capture' or 'process'")

    if capture_type == "process" and not body.get("prompt"):
        raise HTTPException(status_code=400, detail="prompt is required when capture_type is 'process'")

    emergency = body.get("emergency", False)
    priority = PRIORITY_EMERGENCY if emergency else PRIORITY_NORMAL

    meta = dict(body)
    meta["capture_type"] = capture_type

    job_id, is_duplicate, position_info = await enqueue_job(settings, meta, priority=priority)

    status_code = 200 if is_duplicate else 202
    queue_mgr = get_queue_manager()
    job_record = queue_mgr.get_job(job_id)
    response_data = {
        "job_id": job_id,
        "status": job_record.status if job_record else STATUS_QUEUED,
        "url": url,
        "capture_type": capture_type,
        "is_duplicate": is_duplicate,
        "position": position_info,
    }

    if not is_duplicate:
        artifact_url = build_artifact_url(settings, job_id)
        response_data["artifact_url"] = artifact_url

    return JSONResponse(content=response_data, status_code=status_code)


@app.get(
    "/v1/jobs",
    summary="List all jobs",
    description="List all capture jobs, optionally filtered by agent_id or status.",
)
async def list_jobs(agent_id: Optional[str] = Query(default=None)):
    jobs = list_jobs_filtered(agent_id=agent_id)
    return [
        {
            "job_id": j.job_id,
            "status": j.status,
            "doc_name": j.doc_name,
            "agent_id": j.agent_id,
            "client_ref": j.client_ref,
            "artifact_url": j.artifact_url,
            "error": j.error,
        }
        for j in jobs
    ]


@app.get(
    "/v1/jobs/{job_id}",
    summary="Get job status",
    description="Get the current status of a capture job.",
)
async def get_job_status(job_id: str):
    try:
        parse_job_uuid(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    queue_mgr = get_queue_manager()
    position = queue_mgr.get_queue_position(job_id)

    return {
        "job_id": job.job_id,
        "status": job.status,
        "doc_name": job.doc_name,
        "agent_id": job.agent_id,
        "client_ref": job.client_ref,
        "artifact_url": job.artifact_url,
        "error": job.error,
        "position": position,
    }


@app.get(
    "/v1/jobs/{job_id}/position",
    summary="Get queue position",
    description="Get the queue position of a queued job.",
)
async def get_job_position(job_id: str):
    try:
        parse_job_uuid(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    queue_mgr = get_queue_manager()
    position = queue_mgr.get_queue_position(job_id)
    if not position:
        raise HTTPException(status_code=404, detail="Job not found in queue")

    return {"job_id": job_id, "position": position}


@app.post(
    "/v1/jobs/{job_id}/boost",
    summary="Boost job priority",
    description="Move a queued job to the front of the queue (emergency priority).",
)
async def boost_job(job_id: str):
    try:
        parse_job_uuid(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    queue_mgr = get_queue_manager()
    success = queue_mgr.boost_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found or already processing")

    return {"job_id": job_id, "boosted": True}


@app.post(
    "/v1/jobs/{job_id}/retry",
    summary="Retry a failed job",
    description="Re-queue a failed job for processing.",
)
async def retry_job(job_id: str):
    try:
        parse_job_uuid(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    queue_mgr = get_queue_manager()
    success = queue_mgr.retry_failed_job(job_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot retry job (not failed or spool missing)")

    await acquire_slot_for_retried_job()

    return {"job_id": job_id, "retried": True}


@app.delete(
    "/v1/jobs/{job_id}",
    summary="Cancel a job",
    description="Cancel and remove a job from the queue.",
)
async def cancel_job(job_id: str):
    try:
        parse_job_uuid(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    queue_mgr = get_queue_manager()
    success = queue_mgr.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")

    pop_job_record(job_id)
    return {"job_id": job_id, "cancelled": True}


@app.get(
    "/v1/artifacts/{job_id}",
    summary="Download completed artifact",
    description="Get the completed capture artifact JSON for a job.",
)
async def get_artifact(job_id: str):
    try:
        parse_job_uuid(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    settings = get_settings()
    artifact_path = Path(settings.artifact_dir) / f"{job_id}.json"
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")

    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        return JSONResponse(content=data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading artifact: {e}")


@app.delete(
    "/v1/artifacts/{job_id}",
    summary="Remove artifact",
    description="Remove a completed artifact and its registry entry.",
)
async def delete_artifact(job_id: str):
    try:
        parse_job_uuid(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    settings = get_settings()
    remove_completed_artifact(settings, job_id)
    return {"job_id": job_id, "removed": True}


@app.get(
    "/v1/queue",
    summary="View active queue",
    description="View the current active queue (queued and processing jobs).",
)
async def view_queue():
    queue_mgr = get_queue_manager()
    return queue_mgr.get_agent_queue()