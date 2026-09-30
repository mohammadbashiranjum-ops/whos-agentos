"""
Health
======

agno's `/health` answers `{"status": "ok"}` whenever HTTP is up. That hides the
failure that matters most for durable background runs: a lifespan that was
cancelled (or a worker that crashed) leaves the job-queue worker stopped while
every HTTP route keeps serving, so accepted runs sit queued forever and the
healthcheck stays green.

`install_queue_worker_health(app)` puts a check in front of that route: when the
app has a queue worker and it is no longer running, `/health` answers 503. With
no queue configured, or before the lifespan has started it, agno's answer stands.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

HEALTH_PATH = "/health"


def queue_worker_alive(app: Any) -> bool | None:
    """True/False for the app's job-queue worker; None when it has none.

    agno 3.0.4 publishes the worker on `app.state.queue_worker` from its lifespan
    and stops it (`_running` False, poll task cleared) when the lifespan ends.
    """
    worker = getattr(app.state, "queue_worker", None)
    if worker is None:
        return None
    task = getattr(worker, "_task", None)
    return bool(getattr(worker, "_running", False)) and task is not None and not task.done()


def install_queue_worker_health(app: FastAPI) -> None:
    @app.middleware("http")
    async def queue_worker_health(request: Request, call_next: Any) -> Any:
        if request.url.path == HEALTH_PATH and queue_worker_alive(request.app) is False:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": "job queue worker is not running"},
            )
        return await call_next(request)
