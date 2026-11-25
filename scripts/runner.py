import fcntl
import subprocess
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO, Protocol

import psutil

import jobs

DEFAULT_TIMEOUT_SECONDS = 12 * 60 * 60
STOP_GRACE_SECONDS = 60


class Runnable(Protocol):
    @property
    def id(self) -> str: ...

    def argv(self) -> list[str]: ...


def acquire_lock() -> IO[str] | None:
    """Non-blocking exclusive lock, so an overrunning tick cannot collide with the next."""
    jobs.WORK_DIR.mkdir(parents=True, exist_ok=True)
    handle = (jobs.WORK_DIR / "sweep.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def stop_process_tree(process: subprocess.Popen) -> None:
    """SIGTERM every descendant, as `uv run` starts oex-cli as a child; SIGKILL after grace."""
    root = psutil.Process(process.pid)
    tree = [root, *root.children(recursive=True)]
    for member in tree:
        try:
            member.terminate()
        except psutil.NoSuchProcess:
            continue
    _, alive = psutil.wait_procs(tree, timeout=STOP_GRACE_SECONDS)
    for member in alive:
        try:
            member.kill()
        except psutil.NoSuchProcess:
            continue
    process.wait()


class Runner:
    """Runs a batch of jobs and returns the ids of those that failed."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout
        self._done = 0
        self._total = 0
        self._lock = threading.Lock()

    def run(self, batch: Sequence[Runnable], workers: int = 1) -> list[str]:
        self._done, self._total = 0, len(batch)
        if workers == 1:
            return [failed for job in batch if (failed := self.run_one(job, None)) is not None]
        log_dir = jobs.WORK_DIR / "logs"
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                results = list(pool.map(lambda job: self.run_one(job, log_dir), batch))
            except KeyboardInterrupt:
                # Ctrl+C already reached the running jobs; the queued ones must not start.
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        return [failed for failed in results if failed is not None]

    def run_one(self, job: Runnable, log_dir: Path | None) -> str | None:
        """Returns the job id when it fails, None when it succeeds."""
        print(f"{job.id}: starting", flush=True)
        log_file = None
        log_hint = ""
        try:
            if log_dir is not None:
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / f"{job.id.replace('/', '_')}.log"
                log_file = log_path.open("w", encoding="utf-8")
                log_hint = f", see {log_path}"
            process = subprocess.Popen(
                job.argv(), cwd=jobs.REPO_ROOT, stdout=log_file, stderr=log_file
            )
            try:
                returncode = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                stop_process_tree(process)
                print(
                    f"{self._tick()} {job.id} TIMEOUT after {self.timeout}s{log_hint}",
                    file=sys.stderr,
                )
                return job.id
        except OSError as error:
            # One unrunnable job must not discard the results of the jobs beside it.
            print(f"{self._tick()} {job.id} FAILED {error}{log_hint}", file=sys.stderr)
            return job.id
        finally:
            if log_file is not None:
                log_file.close()
        if returncode != 0:
            print(f"{self._tick()} {job.id} FAILED rc={returncode}{log_hint}", file=sys.stderr)
            return job.id
        print(f"{self._tick()} {job.id} done", flush=True)
        return None

    def _tick(self) -> str:
        with self._lock:
            self._done += 1
            return f"[{self._done}/{self._total}]"
