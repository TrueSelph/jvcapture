from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from jvcapture.queue_manager import (
    STATUS_QUEUED,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_WEBHOOK_FAILED,
    PRIORITY_NORMAL,
    PRIORITY_EMERGENCY,
)

logger = logging.getLogger(__name__)

_jobs: Dict[str, "JobRecord"] = {}
_registry_lock = asyncio.Lock()
_job_slots: Optional[asyncio.Semaphore] = None
_capture_semaphore: Optional[asyncio.Semaphore] = None
_webhook_semaphore: Optional[asyncio.Semaphore] = None

_MAX_JOB_REGISTRY_ENTRIES: int = 2000


def configure_job_slots(max_active_jobs: int, max_concurrent_captures: int, max_concurrent_webhooks: int) -> None:
    global _job_slots, _capture_semaphore, _webhook_semaphore
    _job_slots = asyncio.BoundedSemaphore(max(1, max_active_jobs))
    _capture_semaphore = asyncio.Semaphore(max(1, max_concurrent_captures))
    _webhook_semaphore = asyncio.Semaphore(max(1, max_concurrent_webhooks))


async def acquire_slot_for_retried_job() -> None:
    assert _job_slots is not None, "job slots not configured; start jvcapture worker first"
    await _job_slots.acquire()
    logger.info("jvcapture acquired job slot for retried job")


@dataclass
class JobRecord:
    job_id: str
    status: str
    error: Optional[str] = None
    artifact_url: Optional[str] = None
    client_ref: Optional[str] = None
    agent_id: Optional[str] = None
    doc_name: Optional[str] = field(default=None, repr=False)


def _artifact_public_path(job_id: str) -> str:
    return f"/v1/artifacts/{job_id}"


def build_artifact_url(settings, job_id: str) -> str:
    base = (settings.public_base_url or "").strip().rstrip("/")
    rel = _artifact_public_path(job_id)
    if base:
        return f"{base}{rel}"
    return rel


def _spool_meta_path(settings, job_id: str) -> Path:
    spool = Path(settings.artifact_dir) / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    return spool / f"{job_id}.meta.json"


def _artifact_path(settings, job_id: str) -> Path:
    root = Path(settings.artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{job_id}.json"


def resolved_artifact_path(settings, job_id: str) -> Path:
    return _artifact_path(settings, job_id)


def parse_job_uuid(job_id: str) -> str:
    try:
        return str(uuid.UUID(job_id))
    except ValueError as e:
        raise ValueError("Invalid job_id") from e


async def enqueue_job(
    settings,
    meta: Dict[str, Any],
    priority: int = 0,
) -> tuple:
    from jvcapture.queue_manager import get_queue_manager

    assert _job_slots is not None, "job slots not configured; start jvcapture worker first"
    await _job_slots.acquire()
    try:
        doc_name = meta.get("url") or meta.get("doc_name") or "unknown"
        agent_id = meta.get("agent_id")
        client_ref = meta.get("client_ref")
        capture_type = meta.get("capture_type", "capture")
        callback_url = meta.get("callback_url")
        callback_secret = meta.get("callback_secret")

        queue_manager = get_queue_manager()
        job_id, is_duplicate, position_info = queue_manager.enqueue_job(
            doc_name=doc_name,
            spool_meta="",
            agent_id=agent_id,
            priority=priority,
            client_ref=client_ref,
            capture_type=capture_type,
            callback_url=callback_url,
            callback_secret=callback_secret,
        )

        if is_duplicate:
            _job_slots.release()
            logger.info(
                "jvcapture duplicate detected job_id=%s doc_name=%s agent_id=%s",
                job_id,
                doc_name,
                agent_id,
            )
            return job_id, True, position_info

        json_path = _spool_meta_path(settings, job_id)
        json_path.write_text(json.dumps(meta, default=str), encoding="utf-8")

        queue_manager.update_job_status(
            job_id,
            STATUS_QUEUED,
            spool_meta=str(json_path),
        )

        async with _registry_lock:
            _jobs[job_id] = JobRecord(
                job_id=job_id,
                status=STATUS_QUEUED,
                client_ref=client_ref,
                agent_id=agent_id,
                doc_name=doc_name,
            )

        logger.info(
            "jvcapture job queued job_id=%s doc_name=%s capture_type=%s priority=%s position=%s",
            job_id,
            doc_name,
            capture_type,
            priority,
            position_info,
        )

        return job_id, False, position_info
    except Exception:
        _job_slots.release()
        raise


def get_job(job_id: str) -> Optional[JobRecord]:
    from jvcapture.queue_manager import get_queue_manager
    queue_manager = get_queue_manager()
    queue_job = queue_manager.get_job(job_id)
    if queue_job:
        return JobRecord(
            job_id=queue_job.job_id,
            status=queue_job.status,
            error=queue_job.error,
            artifact_url=queue_job.artifact_url,
            client_ref=queue_job.client_ref,
            agent_id=queue_job.agent_id,
            doc_name=queue_job.doc_name,
        )
    if job_id in _jobs:
        return _jobs[job_id]
    return None


def pop_job_record(job_id: str) -> None:
    _jobs.pop(job_id, None)


def remove_completed_artifact(settings, job_id: str) -> None:
    from jvcapture.queue_manager import get_queue_manager
    path = resolved_artifact_path(settings, job_id)
    path.unlink(missing_ok=True)
    pop_job_record(job_id)
    get_queue_manager().discard_completed_job_record(job_id)


def list_jobs_filtered(agent_id: Optional[str] = None) -> list:
    from jvcapture.queue_manager import get_queue_manager
    queue_manager = get_queue_manager()
    all_jobs = queue_manager.list_all_jobs(agent_id=agent_id)

    out: list = []
    for qjob in all_jobs:
        out.append(JobRecord(
            job_id=qjob.job_id,
            status=qjob.status,
            error=qjob.error,
            artifact_url=qjob.artifact_url,
            client_ref=qjob.client_ref,
            agent_id=qjob.agent_id,
            doc_name=qjob.doc_name,
        ))
    return out


def _prune_job_registry() -> None:
    if len(_jobs) <= _MAX_JOB_REGISTRY_ENTRIES:
        return
    for jid, rec in list(_jobs.items()):
        if rec.status in ("completed", "failed", "webhook_failed"):
            _jobs.pop(jid, None)
        if len(_jobs) <= _MAX_JOB_REGISTRY_ENTRIES - 200:
            break


async def _run_capture_with_semaphore(meta: Dict[str, Any]) -> Dict[str, Any]:
    from jvcapture.capture_utils import is_amazon_url, ProxyError
    from jvcapture.capture_amazon import capture_amazon
    from jvcapture.capture_default import capture_default

    async with _capture_semaphore:
        url = meta.get("url", "")

        kwargs = dict(
            url=url,
            max_scrolls=meta.get("max_scrolls", 3),
            headless=True,
            timezone=meta.get("timezone"),
            locale=meta.get("locale"),
            proxy=meta.get("proxy"),
            proxy_timeout=meta.get("proxy_timeout", 20),
            geoip=meta.get("geoip"),
            save_image=meta.get("save_image", False),
        )
        zip_code = meta.get("zip_code")
        if zip_code:
            kwargs["zip_code"] = zip_code

        loop = asyncio.get_event_loop()
        if is_amazon_url(url):
            result = await loop.run_in_executor(None, lambda: capture_amazon(**kwargs))
        else:
            result = await loop.run_in_executor(None, lambda: capture_default(**kwargs))

        return result


async def _run_one_job(settings, job_id: str) -> None:
    from jvcapture.queue_manager import get_queue_manager
    from jvcapture.webhook import post_capture_callback, _append_api_key_query

    queue_manager = get_queue_manager()
    meta_path: Optional[Path] = None

    try:
        job_record = queue_manager.get_job(job_id)
        if not job_record or job_record.job_id != job_id:
            logger.warning("jvcapture job %s not found in queue", job_id)
            return

        meta_path = Path(job_record.spool_meta) if job_record.spool_meta else None

        if not meta_path or not meta_path.exists():
            logger.warning("jvcapture job %s missing spool file", job_id)
            queue_manager.update_job_status(job_id, STATUS_FAILED, error="Missing spool file")
            return

        try:
            raw_meta = meta_path.read_text(encoding="utf-8")
            meta = json.loads(raw_meta)
        except Exception as e:
            queue_manager.update_job_status(job_id, STATUS_FAILED, error=f"Invalid job metadata: {e}")
            logger.exception("jvcapture job %s bad metadata", job_id)
            return

        capture_type = meta.get("capture_type", "capture")
        callback_url = meta.get("callback_url")
        callback_secret = meta.get("callback_secret")
        doc_name = meta.get("url") or meta.get("doc_name") or "unknown"

        logger.info(
            "jvcapture processing start job_id=%s doc_name=%s capture_type=%s",
            job_id,
            doc_name,
            capture_type,
        )

        async def _process_and_notify() -> None:
            result = await _run_capture_with_semaphore(meta)

            image_b64 = base64.b64encode(result["image_bytes"]).decode("utf-8")

            if capture_type == "process":
                from jvcapture.capture_llm import process_image
                llm_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: process_image(
                        image_b64=image_b64,
                        prompt=meta.get("prompt", ""),
                        provider=meta.get("provider"),
                        model=meta.get("model"),
                        temperature=meta.get("temperature"),
                        max_tokens=meta.get("max_tokens"),
                    ),
                )
                artifact_data = {
                    "site": result["site"],
                    "url": result["url"],
                    "image": image_b64,
                    "description": llm_result.get("description"),
                    "tokens_used": llm_result.get("tokens_used"),
                    "image_path": result.get("image_path"),
                    "capture_type": "process",
                }
            else:
                artifact_data = {
                    "site": result["site"],
                    "url": result["url"],
                    "image": image_b64,
                    "image_path": result.get("image_path"),
                    "capture_type": "capture",
                }

            out = _artifact_path(settings, job_id)
            out.write_text(json.dumps(artifact_data, default=str), encoding="utf-8")

            artifact_url = build_artifact_url(settings, job_id)

            logger.info(
                "jvcapture processing done job_id=%s doc_name=%s artifact=%s",
                job_id,
                doc_name,
                artifact_url,
            )

            notify = (callback_url or "").strip()
            if notify:
                capture_url = artifact_url
                api_key = meta.get("api_key")
                if api_key:
                    capture_url = _append_api_key_query(capture_url, api_key)

                imported_ok = await post_capture_callback(
                    notify,
                    callback_secret,
                    capture_url,
                    max_retries=settings.webhook_max_retries,
                    read_timeout=float(settings.webhook_read_timeout_seconds),
                    semaphore=_webhook_semaphore,
                )
                if imported_ok:
                    queue_manager.update_job_status(
                        job_id,
                        STATUS_COMPLETED,
                        artifact_url=artifact_url,
                    )
                    remove_completed_artifact(settings, job_id)
                    logger.info(
                        "jvcapture job_id=%s callback confirmed; artifact removed",
                        job_id,
                    )
                else:
                    queue_manager.update_job_status(
                        job_id,
                        STATUS_WEBHOOK_FAILED,
                        artifact_url=artifact_url,
                    )
                    logger.warning(
                        "jvcapture job_id=%s callback failed; artifact retained",
                        job_id,
                    )
            else:
                queue_manager.update_job_status(
                    job_id,
                    STATUS_COMPLETED,
                    artifact_url=artifact_url,
                )

            _prune_job_registry()

        try:
            limit = settings.max_job_duration_seconds
            if limit > 0:
                await asyncio.wait_for(_process_and_notify(), timeout=float(limit))
            else:
                await _process_and_notify()
        except TimeoutError:
            _artifact_path(settings, job_id).unlink(missing_ok=True)
            queue_manager.update_job_status(
                job_id,
                STATUS_FAILED,
                error=(
                    f"Job exceeded maximum duration ({limit}s; "
                    "JVCAPTURE_MAX_JOB_DURATION_SECONDS, 0=unlimited)."
                ),
            )
            logger.warning(
                "jvcapture job timeout job_id=%s doc_name=%s max_duration_s=%s",
                job_id,
                doc_name,
                limit,
            )
            _prune_job_registry()
        except Exception as e:
            queue_manager.update_job_status(job_id, STATUS_FAILED, error=str(e))
            logger.exception(
                "jvcapture processing failed job_id=%s doc_name=%s error=%s",
                job_id,
                doc_name,
                e,
            )
            _prune_job_registry()
    finally:
        rec = queue_manager.get_job(job_id)
        if rec and rec.status == STATUS_FAILED:
            pass
        else:
            _cleanup_spool(meta_path)
        if _job_slots is not None:
            _job_slots.release()


def _cleanup_spool(meta_path: Optional[Path]) -> None:
    if meta_path and meta_path.exists():
        meta_path.unlink()


async def worker_loop(settings) -> None:
    from jvcapture.queue_manager import get_queue_manager
    queue_manager = get_queue_manager()

    running: set[asyncio.Task] = set()

    while True:
        job_record = queue_manager.dequeue_job()
        if job_record:
            task = asyncio.create_task(
                _run_one_job(settings, job_record.job_id),
                name=f"jvcapture_job_{job_record.job_id}",
            )
            running.add(task)
            task.add_done_callback(running.discard)
        else:
            await asyncio.sleep(0.5)

        if len(running) >= settings.max_concurrent_captures:
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            running -= done


_worker_task: Optional[asyncio.Task] = None


def start_worker(settings) -> None:
    global _worker_task
    if _job_slots is None:
        configure_job_slots(
            settings.max_active_jobs,
            settings.max_concurrent_captures,
            settings.webhook_max_concurrent,
        )
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(
        worker_loop(settings),
        name="jvcapture_process_worker",
    )


async def stop_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None