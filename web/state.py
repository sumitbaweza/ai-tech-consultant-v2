"""Process-wide objects of the web service: the job queue it defers work to."""

from core.jobs import JobQueue, ProcrastinateQueue

_queue: JobQueue | None = None


def set_queue(queue: JobQueue) -> None:
    global _queue
    _queue = queue


def queue() -> JobQueue:
    """The job queue. Tests replace it with a list-backed fake through set_queue()."""
    if _queue is None:
        from web.worker import app

        set_queue(ProcrastinateQueue(app))
    assert _queue is not None
    return _queue
