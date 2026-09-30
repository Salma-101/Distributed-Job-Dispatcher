import time
import socket
import os
import threading
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, case

from app.database import SessionLocal
from app.models.job import Job


WORKER_ID = os.getenv("WORKER_ID", socket.gethostname())


def fibonacci(n: int) -> int:
    if n < 0:
        raise ValueError("n must be non-negative")

    a, b = 0, 1

    for _ in range(n):
        a, b = b, a + b

    return a


def execute_job(job: Job):
    if job.task_type == "sleep":
        seconds = job.payload.get("seconds")

        if not isinstance(seconds, int):
            raise ValueError(
                "Sleep payload must contain integer 'seconds'"
            )

        time.sleep(seconds)

        return {"slept": seconds}
    
    if job.task_type == "fibonacci":
        n = job.payload.get("n")

        if not isinstance(n, int):
            raise ValueError(
                "Fibonacci payload must contain integer 'n'"
            )

        return {"result": fibonacci(n)}

    raise ValueError(f"Unknown task type: {job.task_type}")


def claim_job(db):
    """
    Atomically claim one queued job.

    FOR UPDATE locks the selected row.
    SKIP LOCKED makes another worker skip rows
    already being claimed by another worker.
    """

    job = db.execute(
        select(Job)
        .where(Job.status == "QUEUED")
        .order_by(
            case(
                (Job.priority == "HIGH", 1),
                (Job.priority == "MEDIUM", 2),
                (Job.priority == "LOW", 3),
                else_=4
            ),
            Job.id
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if job is None:
        db.rollback()
        return None

    job.status = "RUNNING"
    job.worker_id = WORKER_ID
    job.started_at = datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)

    db.commit()

    return job

def heartbeat_loop(job_id, stop_event):
    while not stop_event.wait(5):
        db = SessionLocal()

        try:
            job = db.get(Job, job_id)

            if job and job.status == "RUNNING":
                job.heartbeat_at = datetime.now(timezone.utc)
                db.commit()

        except Exception as e:
            db.rollback()
            print(
                f"[WORKER {WORKER_ID}] "
                f"Heartbeat error for job {job_id}: {e}"
            )

        finally:
            db.close()

def process_job(job: Job, db):
    print(f"[WORKER {WORKER_ID}] Starting job {job.id}")

    stop_event = threading.Event()

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(job.id, stop_event),
        daemon=True
    )

    heartbeat_thread.start()

    try:
        result = execute_job(job)
        stop_event.set()

        # Verify that this worker still owns the job.
        db.refresh(job)

        if job.status != "RUNNING" or job.worker_id != WORKER_ID:
            print(
                f"[WORKER {WORKER_ID}] "
                f"Job {job.id} was reclaimed by another worker"
            )
            return

        job.status = "COMPLETED"
        job.result = result
        job.error = None
        job.completed_at = datetime.now(timezone.utc)

        db.commit()

        print(
            f"[WORKER {WORKER_ID}] "
            f"Job {job.id} completed: {result}"
        )

    except Exception as e:
        stop_event.set()
        job.retry_count += 1
        job.error = str(e)

        if job.retry_count <= job.max_retries:
            job.status = "QUEUED"
            job.completed_at = None

            db.commit()

            print(
                f"[WORKER {WORKER_ID}] "
                f"Job {job.id} failed: {e} "
                f"| Retrying "
                f"({job.retry_count}/{job.max_retries})"
            )

        else:
            job.status = "FAILED"
            job.completed_at = datetime.now(timezone.utc)

            db.commit()

            print(
                f"[WORKER {WORKER_ID}] "
                f"Job {job.id} permanently failed: {e} "
                f"| Retries exhausted"
            )

def recover_stale_jobs(db):
    """
    Recover jobs whose worker has stopped sending heartbeats.
    """

    stale_before = datetime.now(timezone.utc) - timedelta(seconds=15)

    stale_jobs = db.execute(
        select(Job)
        .where(
            Job.status == "RUNNING",
            Job.heartbeat_at.is_not(None),
            Job.heartbeat_at < stale_before
        )
        .with_for_update(skip_locked=True)
    ).scalars().all()

    for job in stale_jobs:
        job.retry_count += 1
        job.error = "Worker heartbeat timed out"
        job.heartbeat_at = None
        job.completed_at = None

        if job.retry_count <= job.max_retries:
            job.status = "QUEUED"

            print(
                f"[WORKER {WORKER_ID}] "
                f"Recovered stale job {job.id} "
                f"| Retrying "
                f"({job.retry_count}/{job.max_retries})"
            )

        else:
            job.status = "FAILED"

            print(
                f"[WORKER {WORKER_ID}] "
                f"Job {job.id} permanently failed "
                f"| Worker heartbeat timed out "
                f"| Retries exhausted"
            )

    db.commit()

def worker_loop():
    print(f"[WORKER {WORKER_ID}] Worker started")

    last_recovery_check = 0

    while True:
        db = SessionLocal()

        try:
            now = time.time()

            if now - last_recovery_check >= 10:
                recover_stale_jobs(db)
                last_recovery_check = now

            job = claim_job(db)

            if job:
                process_job(job, db)
            else:
                time.sleep(2)

        except Exception as e:
            db.rollback()
            print(
                f"[WORKER {WORKER_ID}] Error: {e}"
            )
            time.sleep(2)

        finally:
            db.close()


if __name__ == "__main__":
    worker_loop()