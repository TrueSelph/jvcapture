from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_WEBHOOK_FAILED = "webhook_failed"

PRIORITY_NORMAL = 0
PRIORITY_EMERGENCY = 1

DEFAULT_COMPLETED_RETENTION_HOURS = 24
DEFAULT_FAILED_RETENTION_DAYS = 7


@dataclass
class JobRecord:
    job_id: str
    status: str
    doc_name: str
    agent_id: Optional[str] = None
    priority: int = PRIORITY_NORMAL
    enqueued_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    spool_meta: Optional[str] = None
    artifact_url: Optional[str] = None
    error: Optional[str] = None
    client_ref: Optional[str] = None
    capture_type: Optional[str] = None
    callback_url: Optional[str] = None
    callback_secret: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "doc_name": self.doc_name,
            "agent_id": self.agent_id,
            "priority": self.priority,
            "enqueued_at": self.enqueued_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "spool_meta": self.spool_meta,
            "artifact_url": self.artifact_url,
            "error": self.error,
            "client_ref": self.client_ref,
            "capture_type": self.capture_type,
            "callback_url": self.callback_url,
            "callback_secret": self.callback_secret,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobRecord":
        return cls(
            job_id=data["job_id"],
            status=data["status"],
            doc_name=data["doc_name"],
            agent_id=data.get("agent_id"),
            priority=data.get("priority", PRIORITY_NORMAL),
            enqueued_at=data.get("enqueued_at", datetime.utcnow().isoformat()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            spool_meta=data.get("spool_meta"),
            artifact_url=data.get("artifact_url"),
            error=data.get("error"),
            client_ref=data.get("client_ref"),
            capture_type=data.get("capture_type"),
            callback_url=data.get("callback_url"),
            callback_secret=data.get("callback_secret"),
        )


class QueueManager:

    def __init__(
        self,
        artifact_dir: str,
        completed_retention_hours: int = DEFAULT_COMPLETED_RETENTION_HOURS,
        failed_retention_days: int = DEFAULT_FAILED_RETENTION_DAYS,
    ):
        self.artifact_dir = Path(artifact_dir).expanduser().resolve()
        self.queue_file = self.artifact_dir / "queue.json"
        self.lock_file = self.artifact_dir / "queue.json.lock"
        self.completed_retention = timedelta(hours=completed_retention_hours)
        self.failed_retention = timedelta(days=failed_retention_days)

        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        if not self.queue_file.exists():
            self._save_queue({
                "jobs": {},
                "queue_order": [],
                "completed": {},
                "webhook_failed": {},
            })

    def _acquire_lock(self) -> Any:
        lock_fd = open(self.lock_file, 'w')
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        return lock_fd

    def _release_lock(self, lock_fd: Any) -> None:
        if lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()

    def _load_queue(self) -> Dict[str, Any]:
        if not self.queue_file.exists():
            return {
                "jobs": {},
                "queue_order": [],
                "completed": {},
                "webhook_failed": {},
            }

        try:
            with open(self.queue_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("Failed to load queue file: %s", e)
            return {
                "jobs": {},
                "queue_order": [],
                "completed": {},
                "webhook_failed": {},
            }

    def _save_queue(self, data: Dict[str, Any]) -> None:
        fd, temp_path = tempfile.mkstemp(
            suffix='.json',
            prefix='queue_',
            dir=self.artifact_dir,
        )
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            os.rename(temp_path, self.queue_file)
        except Exception as e:
            logger.error("Failed to save queue file: %s", e)
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def enqueue_job(
        self,
        doc_name: str,
        spool_meta: str,
        agent_id: Optional[str] = None,
        priority: int = PRIORITY_NORMAL,
        client_ref: Optional[str] = None,
        capture_type: Optional[str] = None,
        callback_url: Optional[str] = None,
        callback_secret: Optional[str] = None,
    ) -> Tuple[str, bool, Optional[Dict[str, Any]]]:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()

            if agent_id:
                for job_id, job_data in queue_data["jobs"].items():
                    if (job_data.get("agent_id") == agent_id and
                        job_data.get("doc_name") == doc_name and
                        job_data.get("status") in (STATUS_QUEUED, STATUS_PROCESSING)):
                        position_info = self._get_position_info_unsafe(queue_data, job_id)
                        return job_id, True, position_info

            job_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()

            job_record = {
                "job_id": job_id,
                "status": STATUS_QUEUED,
                "doc_name": doc_name,
                "agent_id": agent_id,
                "priority": priority,
                "enqueued_at": now,
                "started_at": None,
                "completed_at": None,
                "spool_meta": spool_meta,
                "artifact_url": None,
                "error": None,
                "client_ref": client_ref,
                "capture_type": capture_type,
                "callback_url": callback_url,
                "callback_secret": callback_secret,
            }

            queue_data["jobs"][job_id] = job_record
            queue_data["queue_order"].append(job_id)
            queue_data["queue_order"] = self._sort_queue_order(
                queue_data["queue_order"], queue_data["jobs"]
            )

            self._save_queue(queue_data)
            position_info = self._get_position_info_unsafe(queue_data, job_id)
            return job_id, False, position_info

        finally:
            self._release_lock(lock_fd)

    def _sort_queue_order(
        self,
        queue_order: List[str],
        jobs: Dict[str, Any],
    ) -> List[str]:
        def sort_key(job_id: str) -> Tuple[int, str]:
            job = jobs.get(job_id, {})
            priority = job.get("priority", PRIORITY_NORMAL)
            enqueued_at = job.get("enqueued_at", "")
            return (-priority, enqueued_at)

        return sorted(queue_order, key=sort_key)

    def _get_position_info_unsafe(
        self,
        queue_data: Dict[str, Any],
        job_id: str,
    ) -> Dict[str, Any]:
        queue_order = queue_data.get("queue_order", [])
        jobs = queue_data.get("jobs", {})

        overall_position = queue_order.index(job_id) + 1 if job_id in queue_order else 0

        job = jobs.get(job_id, {})
        agent_id = job.get("agent_id")
        per_agent_position = 0

        if agent_id:
            agent_jobs = [
                jid for jid in queue_order
                if jobs.get(jid, {}).get("agent_id") == agent_id
            ]
            if job_id in agent_jobs:
                per_agent_position = agent_jobs.index(job_id) + 1

        return {
            "overall": overall_position,
            "per_agent": per_agent_position,
        }

    def get_queue_position(self, job_id: str) -> Optional[Dict[str, Any]]:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            if job_id not in queue_data.get("jobs", {}):
                return None
            return self._get_position_info_unsafe(queue_data, job_id)
        finally:
            self._release_lock(lock_fd)

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()

            if job_id in queue_data.get("jobs", {}):
                return JobRecord.from_dict(queue_data["jobs"][job_id])

            if job_id in queue_data.get("completed", {}):
                data = queue_data["completed"][job_id]
                data["status"] = STATUS_COMPLETED
                return JobRecord.from_dict(data)

            if job_id in queue_data.get("webhook_failed", {}):
                data = queue_data["webhook_failed"][job_id]
                data["status"] = STATUS_WEBHOOK_FAILED
                return JobRecord.from_dict(data)

            return None
        finally:
            self._release_lock(lock_fd)

    def get_agent_queue(self, agent_id: Optional[str] = None) -> List[JobRecord]:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            jobs = queue_data.get("jobs", {})
            eff = (agent_id or "").strip() or None

            agent_jobs: List[JobRecord] = []
            for job_id, job_data in jobs.items():
                if eff is not None and job_data.get("agent_id") != eff:
                    continue
                agent_jobs.append(JobRecord.from_dict(job_data))

            queue_order = queue_data.get("queue_order", [])
            agent_jobs.sort(
                key=lambda j: (
                    queue_order.index(j.job_id) if j.job_id in queue_order else 999999
                )
            )

            return agent_jobs
        finally:
            self._release_lock(lock_fd)

    def dequeue_job(self) -> Optional[JobRecord]:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            queue_order = queue_data.get("queue_order", [])
            jobs = queue_data.get("jobs", {})

            for job_id in queue_order:
                job = jobs.get(job_id)
                if job and job.get("status") == STATUS_QUEUED:
                    job["status"] = STATUS_PROCESSING
                    job["started_at"] = datetime.utcnow().isoformat()
                    self._save_queue(queue_data)
                    return JobRecord.from_dict(job)

            return None
        finally:
            self._release_lock(lock_fd)

    def update_job_status(
        self,
        job_id: str,
        status: str,
        **kwargs: Any,
    ) -> None:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()

            if job_id not in queue_data.get("jobs", {}):
                logger.warning("Job %s not found for status update", job_id)
                return

            job = queue_data["jobs"][job_id]
            job["status"] = status

            for key, value in kwargs.items():
                if key in (
                    "artifact_url",
                    "error",
                    "completed_at",
                    "spool_meta",
                ):
                    job[key] = value

            if status == STATUS_COMPLETED:
                job["completed_at"] = datetime.utcnow().isoformat()
                cleanup_at = (datetime.utcnow() + self.completed_retention).isoformat()
                queue_data["completed"][job_id] = job
                del queue_data["jobs"][job_id]

                if job_id in queue_data["queue_order"]:
                    queue_data["queue_order"].remove(job_id)

                queue_data["completed"][job_id]["cleanup_at"] = cleanup_at

            elif status == STATUS_WEBHOOK_FAILED:
                job["completed_at"] = datetime.utcnow().isoformat()
                cleanup_at = (datetime.utcnow() + self.failed_retention).isoformat()
                queue_data["webhook_failed"][job_id] = job
                del queue_data["jobs"][job_id]

                if job_id in queue_data["queue_order"]:
                    queue_data["queue_order"].remove(job_id)

                queue_data["webhook_failed"][job_id]["cleanup_at"] = cleanup_at

            else:
                queue_data["jobs"][job_id] = job

            self._save_queue(queue_data)
        finally:
            self._release_lock(lock_fd)

    def boost_job(self, job_id: str) -> bool:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            queue_order = queue_data.get("queue_order", [])
            jobs = queue_data.get("jobs", {})

            if job_id not in jobs:
                return False

            job = jobs[job_id]
            if job.get("status") != STATUS_QUEUED:
                return False

            if job_id in queue_order:
                queue_order.remove(job_id)

            insert_position = 0
            for i, jid in enumerate(queue_order):
                j = jobs.get(jid, {})
                if j.get("status") == STATUS_PROCESSING:
                    insert_position = i + 1
                    break

            queue_order.insert(insert_position, job_id)

            job["priority"] = PRIORITY_EMERGENCY
            queue_data["jobs"][job_id] = job
            queue_data["queue_order"] = queue_order

            self._save_queue(queue_data)
            return True
        finally:
            self._release_lock(lock_fd)

    def cancel_job(self, job_id: str) -> bool:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()

            if job_id in queue_data.get("jobs", {}):
                job = queue_data["jobs"][job_id]

                spool_meta = job.get("spool_meta")
                if spool_meta and os.path.exists(spool_meta):
                    os.unlink(spool_meta)

                del queue_data["jobs"][job_id]
                if job_id in queue_data["queue_order"]:
                    queue_data["queue_order"].remove(job_id)

                self._save_queue(queue_data)
                return True

            return False
        finally:
            self._release_lock(lock_fd)

    def retry_failed_job(self, job_id: str) -> bool:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            jobs = queue_data.get("jobs", {})
            if job_id not in jobs:
                return False
            job = jobs[job_id]
            if job.get("status") != STATUS_FAILED:
                return False
            spool_meta = job.get("spool_meta")
            if not spool_meta:
                return False
            if not os.path.exists(spool_meta):
                return False
            job["status"] = STATUS_QUEUED
            job["error"] = None
            job["started_at"] = None
            queue_order = queue_data.get("queue_order", [])
            if job_id not in queue_order:
                queue_order.append(job_id)
            queue_data["queue_order"] = self._sort_queue_order(
                queue_order, queue_data["jobs"]
            )
            queue_data["jobs"][job_id] = job
            self._save_queue(queue_data)
            return True
        finally:
            self._release_lock(lock_fd)

    def check_duplicate(
        self,
        doc_name: str,
        agent_id: Optional[str],
    ) -> Optional[JobRecord]:
        if not agent_id:
            return None

        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            jobs = queue_data.get("jobs", {})

            for job_data in jobs.values():
                if (job_data.get("agent_id") == agent_id and
                    job_data.get("doc_name") == doc_name and
                    job_data.get("status") in (STATUS_QUEUED, STATUS_PROCESSING)):
                    return JobRecord.from_dict(job_data)

            return None
        finally:
            self._release_lock(lock_fd)

    def cleanup_completed_jobs(self) -> int:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            now = datetime.utcnow()
            removed_count = 0

            jobs_to_remove = []
            for job_id, job_data in queue_data.get("completed", {}).items():
                cleanup_at_str = job_data.get("cleanup_at")
                if cleanup_at_str:
                    cleanup_at = datetime.fromisoformat(cleanup_at_str)
                    if now > cleanup_at:
                        jobs_to_remove.append(job_id)

            for job_id in jobs_to_remove:
                job_data = queue_data["completed"][job_id]
                artifact_url = job_data.get("artifact_url")
                if artifact_url:
                    artifact_path = self.artifact_dir / f"{job_id}.json"
                    if artifact_path.exists():
                        artifact_path.unlink()

                del queue_data["completed"][job_id]
                removed_count += 1
                logger.info("Cleaned up completed job %s", job_id)

            if removed_count > 0:
                self._save_queue(queue_data)

            return removed_count
        finally:
            self._release_lock(lock_fd)

    def cleanup_webhook_failed_jobs(self) -> int:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            now = datetime.utcnow()
            removed_count = 0

            jobs_to_remove = []
            for job_id, job_data in queue_data.get("webhook_failed", {}).items():
                cleanup_at_str = job_data.get("cleanup_at")
                if cleanup_at_str:
                    cleanup_at = datetime.fromisoformat(cleanup_at_str)
                    if now > cleanup_at:
                        jobs_to_remove.append(job_id)

            for job_id in jobs_to_remove:
                job_data = queue_data["webhook_failed"][job_id]
                artifact_url = job_data.get("artifact_url")
                if artifact_url:
                    artifact_path = self.artifact_dir / f"{job_id}.json"
                    if artifact_path.exists():
                        artifact_path.unlink()

                del queue_data["webhook_failed"][job_id]
                removed_count += 1
                logger.info("Cleaned up webhook failed job %s", job_id)

            if removed_count > 0:
                self._save_queue(queue_data)

            return removed_count
        finally:
            self._release_lock(lock_fd)

    def discard_completed_job_record(self, job_id: str) -> bool:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            completed = queue_data.get("completed", {})
            if job_id not in completed:
                return False
            del completed[job_id]
            queue_data["completed"] = completed
            self._save_queue(queue_data)
            return True
        finally:
            self._release_lock(lock_fd)

    def list_all_jobs(
        self,
        agent_id: Optional[str] = None,
        status_filter: Optional[List[str]] = None,
    ) -> List[JobRecord]:
        lock_fd = self._acquire_lock()
        try:
            queue_data = self._load_queue()
            all_jobs = []

            for job_data in queue_data.get("jobs", {}).values():
                all_jobs.append(JobRecord.from_dict(job_data))

            for job_data in queue_data.get("completed", {}).values():
                job_data_copy = dict(job_data)
                job_data_copy["status"] = STATUS_COMPLETED
                all_jobs.append(JobRecord.from_dict(job_data_copy))

            for job_data in queue_data.get("webhook_failed", {}).values():
                job_data_copy = dict(job_data)
                job_data_copy["status"] = STATUS_WEBHOOK_FAILED
                all_jobs.append(JobRecord.from_dict(job_data_copy))

            if agent_id:
                all_jobs = [j for j in all_jobs if j.agent_id == agent_id]

            if status_filter:
                all_jobs = [j for j in all_jobs if j.status in status_filter]

            return all_jobs
        finally:
            self._release_lock(lock_fd)


_queue_manager: Optional[QueueManager] = None


def get_queue_manager() -> QueueManager:
    if _queue_manager is None:
        raise RuntimeError("QueueManager not initialized. Call init_queue_manager() first.")
    return _queue_manager


def init_queue_manager(
    artifact_dir: str,
    completed_retention_hours: int = DEFAULT_COMPLETED_RETENTION_HOURS,
    failed_retention_days: int = DEFAULT_FAILED_RETENTION_DAYS,
) -> QueueManager:
    global _queue_manager
    _queue_manager = QueueManager(
        artifact_dir=artifact_dir,
        completed_retention_hours=completed_retention_hours,
        failed_retention_days=failed_retention_days,
    )
    return _queue_manager


def reset_queue_manager() -> None:
    global _queue_manager
    _queue_manager = None