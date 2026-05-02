# background_worker.py — Centralized Priority Job Queue
# Replaces scattered threading.Thread spawns with a single worker thread.
# Jobs are prioritized: lower number = higher priority (1 = highest).

import queue
import threading
import itertools


# Priority levels (lower number = higher priority)
PRIORITY_CRITICAL = 1  # Memory extraction (owner data, must be fast)
PRIORITY_HIGH = 2  # Stream summary, important updates
PRIORITY_NORMAL = 3  # Diary, consolidation, non-critical background tasks

_job_queue = queue.PriorityQueue()
_job_counter = itertools.count()


def _worker_loop():
    """Background thread: fetch jobs by priority and execute."""
    while True:
        try:
            priority, _seq, job = _job_queue.get()
            func, args, kwargs = job
            try:
                func(*args, **kwargs)
            except Exception as e:
                print(f"[BgWorker] Job error (priority {priority}): {e}")
            finally:
                _job_queue.task_done()
        except Exception:
            # Avoid thread death on unexpected error
            pass


# Start daemon worker at import time
_thread = threading.Thread(target=_worker_loop, daemon=True)
_thread.start()


def enqueue(priority: int, func, *args, **kwargs):
    """
    Enqueue a job for background execution.
    Lower priority number = higher urgency.

    Use priority constants: PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL
    """
    _job_queue.put((priority, next(_job_counter), (func, args, kwargs)))

def get_queue_stats():
    """Returns the current number of pending jobs in the queue."""
    return _job_queue.qsize()
