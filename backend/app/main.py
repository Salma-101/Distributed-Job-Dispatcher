from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import Literal

from app.database import get_db
from app.models.job import Job
from app.schemas.job import JobCreate, JobResponse


app = FastAPI(
    title="JobDispatcher",
    description="Distributed job scheduling and execution system",
    version="1.0.0"
)


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.post("/jobs", response_model=JobResponse)
def create_job(
    job_data: JobCreate,
    db: Session = Depends(get_db)
):
    job = Job(
        task_type=job_data.task_type,
        payload=job_data.payload,
        priority=job_data.priority,
    )

    db.add(job)
    db.commit()
    db.refresh(job)

    return job


@app.get("/jobs", response_model=list[JobResponse])
def list_jobs(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"] | None = Query(default=None),
    priority: Literal["HIGH", "MEDIUM", "LOW"] | None = Query(default=None),
    db: Session = Depends(get_db)
):
    query = select(Job)

    if status is not None:
        query = query.where(Job.status == status.upper())

    if priority is not None:
        query = query.where(Job.priority == priority.upper())

    query = (
        query
        .order_by(Job.id.desc())
        .offset(offset)
        .limit(limit)
    )

    jobs = db.execute(query).scalars().all()

    return jobs


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: int,
    db: Session = Depends(get_db)
):
    job = db.get(Job, job_id)

    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    return job