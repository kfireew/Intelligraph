"""
build_queue.py — Threaded build queue for clone/pull operations.

Prevents parallel graphify+CRG builds from OOMing the container.
Workers execute build jobs serially (or with limited concurrency).

Configuration:
    INTELLIGRAPH_BUILD_WORKERS=2   — number of concurrent build workers
"""

import os
import logging
import queue
import threading

log = logging.getLogger(__name__)

MAX_WORKERS = int(os.environ.get("INTELLIGRAPH_BUILD_WORKERS", "2"))


class BuildQueue:
    """Simple FIFO queue with N worker threads.

    Jobs are plain callables. Exceptions are logged, never propagated
    (the caller flips project status to 'error' on failure).
    """

    def __init__(self, workers=MAX_WORKERS):
        self._q = queue.Queue()
        self._workers = workers
        self._threads = []
        self._started = False
        self._lock = threading.Lock()

    def start(self):
        """Start worker threads. Safe to call multiple times."""
        with self._lock:
            if self._started:
                return
            for i in range(self._workers):
                t = threading.Thread(
                    target=self._loop,
                    daemon=True,
                    name=f"intelligraph-build-{i}",
                )
                t.start()
                self._threads.append(t)
            self._started = True
            log.info("BuildQueue started with %d workers", self._workers)

    def submit(self, fn, *args, **kwargs):
        """Enqueue a build job. Starts workers if not yet started."""
        if not self._started:
            self.start()
        self._q.put((fn, args, kwargs))
        log.debug("Build job enqueued (queue depth: %d)", self._q.qsize())

    def _loop(self):
        """Worker loop — runs forever, processing jobs."""
        while True:
            fn, args, kwargs = self._q.get()
            try:
                fn(*args, **kwargs)
            except Exception as e:
                log.exception("Build job failed: %s", e)
            finally:
                self._q.task_done()
    @property
    def depth(self):
        """Current queue depth (pending jobs)."""
        return self._q.qsize()


build_queue = BuildQueue()
