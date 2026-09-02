"""Background upload jobs.

Extraction takes tens of seconds per document because it waits on LlamaParse,
Gemini, and Groq in turn. Running it inside the upload request meant the
browser sat on a blank page for the whole batch. Uploads now save the files,
hand them to a worker thread, and redirect to a progress page that polls.

State lives in memory, so it is per-process and cleared on restart. That is
enough for a single-process POC; a multi-worker deployment would need the job
table in the database instead.
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

# Documents are processed a few at a time. Each one is mostly waiting on an
# API, so this is about provider rate limits rather than local CPU.
MAX_PARALLEL_FILES = 3

# Finished jobs are kept this long so the results page survives a refresh.
JOB_RETENTION_SECONDS = 3600

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def create_job(saved_files: list[tuple]) -> str:
    """Register a job for a list of (saved_path, original_name) pairs."""
    job_id = uuid.uuid4().hex

    with _lock:
        _prune_locked()
        _jobs[job_id] = {
            "id": job_id,
            "status": "running",
            "total": len(saved_files),
            "finished": 0,
            "created_at": time.time(),
            "completed_at": None,
            "files": [
                {
                    "file_name": name,
                    "status": "queued",
                    "detail": None,
                    "started_at": None,
                    "finished_at": None,
                    "elapsed_seconds": None,
                }
                for _, name in saved_files
            ],
            "results": [],
        }

    return job_id


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job else None


def start_job(app, job_id: str, saved_files: list[tuple]) -> None:
    """Run a job on a background thread and return immediately."""
    thread = threading.Thread(
        target=_run_job,
        args=(app, job_id, saved_files),
        name=f"upload-{job_id[:8]}",
        daemon=True,
    )
    thread.start()


def _run_job(app, job_id: str, saved_files: list[tuple]) -> None:
    try:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FILES) as pool:
            for index, (saved_path, original_name) in enumerate(saved_files):
                pool.submit(_process_one, app, job_id, index, saved_path, original_name)
    finally:
        _finish_job(app, job_id)


def _process_one(app, job_id, index, saved_path, original_name) -> None:
    """Process one document. Each worker needs its own app context for the DB."""
    from vim.extraction.service import process_saved_file

    _update_file(job_id, index, status="processing")

    try:
        # A fresh app context per thread; Flask-SQLAlchemy scopes its session to
        # the context, so without this the threads would share one session.
        with app.app_context():
            from vim_database.database import db

            try:
                record = process_saved_file(saved_path, original_name)
            finally:
                db.session.remove()

        detail = (
            record.get("_extraction_error")
            or record.get("_db_error")
            or record.get("_incomplete_reason")
            or record.get("_not_invoice_reason")
        )
        _update_file(
            job_id, index,
            status=record.get("status") or "done",
            detail=detail,
            record=record,
        )

    except Exception as e:
        _update_file(job_id, index, status="error", detail=str(e))


def _update_file(job_id, index, *, status, detail=None, record=None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return

        entry = job["files"][index]
        now = time.time()
        entry["status"] = status
        if status == "processing" and not entry.get("started_at"):
            entry["started_at"] = now
        if detail:
            entry["detail"] = str(detail)[:300]

        if status != "processing":
            entry["finished_at"] = now
            started = entry.get("started_at") or now
            entry["elapsed_seconds"] = round(now - started, 1)
            # Only increment the counter once per file — guard against
            # _update_file being called a second time with a terminal status
            # (e.g. on an error path after the first terminal call).
            if entry.get("_counted") is not True:
                entry["_counted"] = True
                job["finished"] += 1
            if record is not None:
                record["_elapsed_seconds"] = entry["elapsed_seconds"]
                job["results"].append(record)


def _finish_job(app, job_id: str) -> None:
    """Mark the job complete and run validation once for the whole batch."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        results = list(job["results"])

    invoice_ids = [
        int(r["invoice_id"]) for r in results if r.get("invoice_id") is not None
    ]

    validation_error = None
    if invoice_ids:
        try:
            with app.app_context():
                from vim_database.database import db
                from vim.validation_setup.validation.run_validation import run_validation

                try:
                    run_validation(invoice_ids=invoice_ids if invoice_ids else None)
                finally:
                    db.session.remove()
        except Exception as e:
            validation_error = str(e)

    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["status"] = "complete"
        job["completed_at"] = time.time()
        job["elapsed_seconds"] = round(job["completed_at"] - job["created_at"], 1)
        job["invoice_ids"] = invoice_ids
        job["validation_error"] = validation_error


def _prune_locked() -> None:
    """Drop jobs that finished long enough ago to be uninteresting."""
    cutoff = time.time() - JOB_RETENTION_SECONDS
    stale = [
        key for key, job in _jobs.items()
        if job.get("completed_at") and job["completed_at"] < cutoff
    ]
    for key in stale:
        del _jobs[key]


def _snapshot(job: dict) -> dict:
    """Copy far enough that callers cannot mutate live job state."""
    return {
        **job,
        "files": [dict(f) for f in job["files"]],
        "results": list(job["results"]),
    }
