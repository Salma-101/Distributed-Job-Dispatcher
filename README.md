# Distributed Job Dispatcher

A **distributed job processing system that tolerates worker failures**, built with **FastAPI, PostgreSQL, SQLAlchemy, and Python workers**.

The system provides a persistent job queue where multiple workers can safely claim and execute jobs concurrently, with **priority scheduling, retries, heartbeats, and stale-worker recovery**.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API-green?logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Database-blue?logo=postgresql)](https://www.postgresql.org/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-ORM-red)](https://www.sqlalchemy.org/)

## Architecture

```text
                ┌──────────────┐
                │    Client    │
                └──────┬───────┘
                       │ REST API
                       ▼
                ┌──────────────┐
                │   FastAPI    │
                └──────┬───────┘
                       │
                       ▼
              ┌──────────────────┐
              │    PostgreSQL    │
              │   Job Queue/DB   │
              └───────┬──────────┘
                      │
              ┌───────┴───────┐
              ▼               ▼
        ┌──────────┐    ┌──────────┐
        │ Worker A │    │ Worker B │
        └──────────┘    └──────────┘
```

The API only writes jobs to PostgreSQL. Workers are independent processes that poll the same table, claim jobs, run them, and write results back. There is no separate broker or scheduler process.

## Key Features

* **Persistent job queue** backed by PostgreSQL
* **Concurrent workers** with safe job claiming
* `FOR UPDATE SKIP LOCKED` for database-level concurrency control
* **Priority-based scheduling** (`HIGH` → `MEDIUM` → `LOW`) with FIFO ordering inside each priority
* Job lifecycle tracking: `QUEUED → RUNNING → COMPLETED / FAILED`
* **Automatic retries:** Failed jobs are retried up to 3 times after the initial attempt, allowing up to 4 total execution attempts before being marked `FAILED`.
* **Worker ownership & heartbeat tracking**
* **Stale-worker detection and job recovery**
* Ownership check before a worker records a result, so a worker whose job was reclaimed does not overwrite the newer execution
* Strict request validation (task types and payloads are validated with Pydantic)
* REST API with automatic Swagger documentation

## Job Lifecycle

```text
                 ┌────────┐
                 │ QUEUED │◄──────────────────────────────┐
                 └───┬────┘                               │
                     │ claimed by a worker                │ yes: requeue
                     ▼                                    │
                ┌─────────┐                               │
                │ RUNNING │                               │
                └────┬────┘                               │
            ┌────────┴─────────┐                          │
            │ success          │ error / heartbeat        │
            ▼                  ▼ timeout                  │
      ┌───────────┐     retry_count += 1                  │
      │ COMPLETED │     retry_count <= max_retries (3)? ──┘
      └───────────┘            │
                               no
                               ▼
                          ┌────────┐
                          │ FAILED │
                          └────────┘
```

Every job gets one **initial attempt**. If an attempt fails, `retry_count` is incremented. While `retry_count` is still within `max_retries` (default **3**), the job goes back to `QUEUED` and is retried. That allows up to **4 total execution attempts**. If all 4 attempts fail, the job is permanently marked `FAILED`.

| Attempt | Type            | `retry_count` after failure | Outcome                     |
| ------- | --------------- | --------------------------- | --------------------------- |
| 1       | Initial attempt | 1                           | Requeued                    |
| 2       | Retry 1         | 2                           | Requeued                    |
| 3       | Retry 2         | 3                           | Requeued                    |
| 4       | Retry 3         | 4                           | Marked `FAILED` permanently |

A worker crash or heartbeat timeout (see [Failure Recovery](#failure-recovery)) counts as a failed attempt and uses the same retry budget.

## Tech Stack

| Technology    | Role                         |
| ------------- | ---------------------------- |
| Python        | Core application & workers   |
| FastAPI       | REST API                     |
| PostgreSQL    | Persistent queue & state     |
| SQLAlchemy    | ORM / database layer         |
| Pydantic      | Request/response validation  |
| Uvicorn       | ASGI server                  |
| python-dotenv | Environment configuration    |
| Docker        | Local PostgreSQL             |

## API

Interactive documentation is available at `/docs` while the API is running.

| Method | Endpoint      | Description                                     |
| ------ | ------------- | ----------------------------------------------- |
| `POST` | `/jobs`       | Create a job                                    |
| `GET`  | `/jobs`       | List jobs (newest first), with optional filters |
| `GET`  | `/jobs/{id}`  | Get one job (`404` if it does not exist)        |
| `GET`  | `/health`     | Health check                                    |

### Create Job

```http
POST /jobs
```

```json
{
  "task_type": "fibonacci",
  "payload": { "n": 40 },
  "priority": "HIGH"
}
```

`priority` is optional and defaults to `MEDIUM`.

Supported task types:

| `task_type` | Payload                  | Result                      |
| ----------- | ------------------------ | --------------------------- |
| `fibonacci` | `{"n": <int >= 0>}`      | `{"result": <fibonacci(n)>}`|
| `sleep`     | `{"seconds": <int >= 0>}`| `{"slept": <seconds>}`      |

Invalid task types or payloads are rejected by the API with a validation error.

### List Jobs

```http
GET /jobs?status=QUEUED&priority=HIGH&limit=50&offset=0
```

| Parameter  | Values                                       | Default |
| ---------- | -------------------------------------------- | ------- |
| `status`   | `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`   | all     |
| `priority` | `HIGH`, `MEDIUM`, `LOW`                      | all     |
| `limit`    | 1–100                                        | 50      |
| `offset`   | ≥ 0                                          | 0       |

### Job fields

Each job returns `id`, `task_type`, `payload`, `status`, `priority`, `result`, `error`, `retry_count`, `max_retries`, `worker_id`, `created_at`, `started_at`, `completed_at`, and `heartbeat_at`.

## Concurrency

Workers safely compete for jobs using PostgreSQL row-level locking. The claim query is equivalent to:

```sql
SELECT *
FROM jobs
WHERE status = 'QUEUED'
ORDER BY
  CASE priority
    WHEN 'HIGH'   THEN 1
    WHEN 'MEDIUM' THEN 2
    WHEN 'LOW'    THEN 3
    ELSE 4
  END,
  id
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

`SKIP LOCKED` lets a worker skip rows currently locked by another worker instead of blocking. The selected row is then updated to `RUNNING` (with `worker_id`, `started_at`, and `heartbeat_at`) in the same transaction, so two workers cannot claim the same queued job.

Within a priority level, jobs run in creation order (ascending `id`).

## Failure Recovery

While a job runs, the worker's background thread updates `heartbeat_at` on that job every few seconds. Recovery is done by the workers themselves: each worker periodically scans for `RUNNING` jobs whose heartbeat is stale and either requeues them or fails them, using the same retry budget described above.

```text
Worker A claims Job 42
        ↓
Worker A stops sending heartbeats
        ↓
Another worker's recovery scan finds Job 42 stale
        ↓
Job 42 is requeued (or marked FAILED if retries are exhausted)
        ↓
Worker B claims Job 42
```

Before a worker records a successful result, it re-reads the job and checks that the job is still `RUNNING` and still owned by its own `worker_id`. If the job was reclaimed in the meantime, the worker discards its result instead of overwriting the newer execution.

### Delivery guarantee

Because stale jobs are requeued and retried, the system provides **at-least-once execution**: a job whose worker stalled (but did not actually die) may be executed again by another worker. Task implementations should therefore be **idempotent**.

### Timing

| Setting                  | Value        |
| ------------------------ | ------------ |
| Heartbeat interval       | 5 seconds    |
| Job considered stale     | 15 seconds without a heartbeat |
| Recovery scan (per worker)| every 10 seconds |
| Idle polling interval    | 2 seconds    |

These values are currently constants in `backend/app/worker.py`.

## Configuration

| Variable       | Required | Description                                              |
| -------------- | -------- | -------------------------------------------------------- |
| `DATABASE_URL` | Yes      | SQLAlchemy connection string for PostgreSQL              |
| `WORKER_ID`    | No       | Name shown on claimed jobs. Defaults to the machine hostname |

## Project Structure

```text
Distributed-Job-Dispatcher/
├── backend/
│   ├── .env.example        # Template for local configuration
│   └── app/
│       ├── main.py         # FastAPI app and routes
│       ├── worker.py       # Worker: claim, execute, heartbeat, recovery
│       ├── database.py     # Engine, session, Base
│       ├── models/job.py   # Job ORM model
│       └── schemas/job.py  # Pydantic request/response schemas
├── requirements.txt
├── .gitignore
└── README.md
```

## Running Locally

All `backend` commands below are run from the `backend/` directory, because the code imports from the `app` package.

### 1. Clone and install

```bash
git clone https://github.com/Salma-101/Distributed-Job-Dispatcher.git
cd Distributed-Job-Dispatcher

python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Start PostgreSQL

Replace the `<...>` placeholders with values of your choice (use a strong, unique password):

```bash
docker run --name job-dispatcher-db \
  -e POSTGRES_USER=<db_user> \
  -e POSTGRES_PASSWORD=<db_password> \
  -e POSTGRES_DB=<db_name> \
  -p 5432:5432 \
  -d postgres:16
```

### 3. Configure the environment

Copy the template and fill in the same values you used above:

```bash
cp backend/.env.example backend/.env      # Windows: copy backend\.env.example backend\.env
```

```env
DATABASE_URL=postgresql+psycopg2://<db_user>:<db_password>@127.0.0.1:5432/<db_name>
```

> **Do not commit `.env` or real database credentials to the repository.** It is already listed in `.gitignore`.

### 4. Create the database table

```bash
cd backend
python -c "from app.database import Base, engine; from app.models.job import Job; Base.metadata.create_all(engine)"
```

### 5. Start the API

```bash
cd backend
uvicorn app.main:app --reload
```

### 6. Start one or more workers

Run each worker in its own terminal. Give each one a distinct `WORKER_ID` so you can see which worker claimed which job:

```bash
cd backend

# macOS / Linux
WORKER_ID=worker-1 python -m app.worker

# Windows (PowerShell)
$env:WORKER_ID="worker-1"; python -m app.worker
```

### 7. Try it

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"task_type": "fibonacci", "payload": {"n": 40}, "priority": "HIGH"}'

curl http://127.0.0.1:8000/jobs/1
```

To see recovery in action, queue a long job, e.g. `{"task_type": "sleep", "payload": {"seconds": 60}}`, stop the worker that claims it (Ctrl+C), and watch another worker requeue and run it after the heartbeat times out.

## Project Status

* [x] FastAPI API
* [x] PostgreSQL persistence
* [x] SQLAlchemy integration
* [x] Multi-worker execution
* [x] Priority scheduling
* [x] Concurrent job claiming
* [x] Worker heartbeats
* [x] Stale-worker recovery
* [x] Retry/failure handling (up to 3 retries after the initial attempt; 4 total attempts)
* [x] Request validation
* [x] Docker-based PostgreSQL setup

### Planned

* [ ] Automated test suite
* [ ] Exponential retry backoff
* [ ] Dead-letter queue
* [ ] Job cancellation
* [ ] Metrics & observability
* [ ] Authentication
* [ ] CI/CD
* [ ] Production deployment

## Why This Project?

This project explores the core problems behind distributed job-processing systems:

**How do multiple workers safely share a queue?**

**What happens when a worker fails mid-execution?**

**How can stale workers be prevented from corrupting newer job state?**

Rather than relying on a dedicated message broker, the project uses **PostgreSQL transactions, row-level locking, worker ownership, and heartbeat-based recovery** to implement these mechanisms.

## Author

**Salma Aslam**

B.Tech Computer Science — PES University

[GitHub](https://github.com/Salma-101)
