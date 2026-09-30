# Job Dispatcher

A distributed job scheduling and execution system built with **FastAPI, PostgreSQL, and Python workers**.

The system accepts jobs through a REST API, stores them in PostgreSQL as a persistent queue, and allows multiple workers to safely claim and execute jobs concurrently.

## Architecture

```text
                    ┌──────────────┐
                    │    Client    │
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐
                    │   FastAPI    │
                    │     API      │
                    └──────┬───────┘
                           │
                           ▼
                 ┌────────────────────┐
                 │    PostgreSQL      │
                 │    Jobs Queue      │
                 └─────────┬──────────┘
                           │
                ┌──────────┴──────────┐
                │                     │
                ▼                     ▼
        ┌──────────────┐      ┌──────────────┐
        │   Worker 1   │      │   Worker 2   │
        │    salma     │      │  worker-2    │
        └──────┬───────┘      └──────┬───────┘
               │                     │
               └──────────┬──────────┘
                          ▼
                  Execute Job
                          │
                          ▼
              COMPLETED / QUEUED / FAILED