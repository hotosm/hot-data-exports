import contextlib
import fcntl
import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.process import BaseProcess
from typing import IO

from oex.catalog import S3CatalogStore
from oex.config.loader import load_config
from oex.exporter import Exporter
from oex.hdx_publisher import HdxPublisher
from oex.osm.runner import OsmRunner
from oex.overture.runner import OvertureRunner

import jobs
from events import CLOSE_COMMAND, closed_key
from jobs import Job, ScheduleError

DEFAULT_TIMEOUT_SECONDS = 12 * 60 * 60
STOP_GRACE_SECONDS = 60
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
SOURCES = {"osm": OsmRunner, "overture": OvertureRunner}
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# Workers start jobs from threads, and forking a threaded process can deadlock the child.
SPAWN = multiprocessing.get_context("spawn")


def acquire_lock(wait: bool = False) -> IO[str] | None:
    """Exclusive lock, so two sweeps on one machine never overlap; None if held and not waiting."""
    jobs.WORK_DIR.mkdir(parents=True, exist_ok=True)
    handle = (jobs.WORK_DIR / "sweep.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def export(job: Job) -> bool:
    """One job through oex; True when every layer succeeded."""
    cfg = load_config(job.config, job.overrides())
    if job.command != CLOSE_COMMAND:
        result = Exporter(cfg, SOURCES[job.command]()).run()
        logging.getLogger("sweep").info(
            "%d ok, %d unchanged, %d empty, %d skipped, %d failed in %.1fs",
            result.succeeded,
            result.unchanged,
            result.empty,
            result.skipped,
            result.failed,
            result.total_duration_s,
        )
        return result.failed == 0
    HdxPublisher(cfg.hdx).update_metadata(cfg)
    key = closed_key(cfg)
    if key is None:
        raise ScheduleError(f"{job.id}: no STAC catalog on S3 to record the close in")
    store = S3CatalogStore(cfg)
    store.write(key, {"expires": job.closes}, store.read(key)[1])
    return True


def _child(target: Callable[[Job], bool], job: Job) -> None:
    # Its own process group, so a timeout also stops the tippecanoe and osmium oex starts.
    os.setpgrp()
    logging.basicConfig(format=f"{job.id} {LOG_FORMAT}", force=True)
    try:
        succeeded = target(job)
    except Exception:
        # Any exception fails only this job; logging it here puts the reason under the job's id.
        logging.getLogger("sweep").exception("%s FAILED", job.id)
        sys.exit(1)
    sys.exit(0 if succeeded else 1)


def _signal(process: BaseProcess, sig: int) -> None:
    if process.pid is not None:
        # The group is already gone once every process in it has exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
    # A job that just started may not have made its group yet.
    if process.exitcode is None:
        (process.terminate if sig == signal.SIGTERM else process.kill)()


def stop(processes: Sequence[BaseProcess]) -> None:
    """SIGTERM every job's process group at once, SIGKILL what is left after the grace."""
    for process in processes:
        _signal(process, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    for process in processes:
        process.join(max(0.0, deadline - time.monotonic()))
    for process in processes:
        _signal(process, signal.SIGKILL)
        process.join()


class Runner:
    """Runs each job in a process of its own and returns the ids of those that failed."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        target: Callable[[Job], bool] = export,
    ):
        self.timeout = timeout
        self.target = target
        self._lock = threading.Lock()
        self._running: set[BaseProcess] = set()
        self._stopping = threading.Event()

    def run(self, batch: Sequence[Job], workers: int = 1) -> list[str]:
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            results = list(pool.map(self.run_one, batch))
        except KeyboardInterrupt:
            # Jobs run in their own process groups, so Ctrl+C reaches only the sweep.
            pool.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                self._stopping.set()
                running = list(self._running)
            # A second Ctrl+C must not cut the stop short and leave a job running.
            previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in STOP_SIGNALS}
            try:
                stop(running)
                pool.shutdown()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            raise
        pool.shutdown()
        return [failed for failed in results if failed is not None]

    def run_one(self, job: Job) -> str | None:
        print(f"{job.id}: starting", flush=True)
        process = SPAWN.Process(target=_child, args=(self.target, job), name=job.id)
        with self._lock:
            stopping = self._stopping.is_set()
            if not stopping:
                process.start()
                self._running.add(process)
        if stopping:
            print(f"{job.id} STOPPED", file=sys.stderr)
            return job.id
        try:
            process.join(self.timeout)
            if self._stopping.is_set():
                # run() is stopping it; waiting here too keeps the exit code from racing.
                process.join()
                print(f"{job.id} STOPPED", file=sys.stderr)
                return job.id
            if process.exitcode is None:
                stop([process])
                print(f"{job.id} TIMEOUT after {self.timeout}s", file=sys.stderr)
                return job.id
        finally:
            with self._lock:
                self._running.discard(process)
        if process.exitcode != 0:
            print(f"{job.id} FAILED rc={process.exitcode}", file=sys.stderr)
            return job.id
        print(f"{job.id} done", flush=True)
        return None
