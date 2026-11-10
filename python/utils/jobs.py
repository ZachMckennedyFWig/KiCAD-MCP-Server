"""Background jobs for long-running tools (MCP_ISSUES principle 6, #5m).

A tool that can take minutes (autorouting, a database download, a fab package)
should not hold the single command channel open past the bridge's timeout.
Instead it starts a job and returns ``{"jobId": ...}`` at once; the caller then
polls ``job_status`` and, for jobs that change the board, calls ``job_apply``.

Design
------
* A job is a Python callable run in a daemon thread of this worker process. The
  callable gets a :class:`JobContext` to report progress, append log lines,
  check for cancellation and register child processes (killed on cancel).
* Jobs never touch the in-memory board. Board work (pcbnew) is done in child
  processes on *copies* of the board file, because SWIG/pcbnew is not
  thread-safe and the main command loop keeps using the board while the job
  runs. Anything that must change the user's board is returned as a pending
  *apply step*: ``job.apply_fn`` is called by ``job_apply`` on the main command
  thread, so it is serialised with every other board mutation without a lock.
* ``stalled`` is reported (and, if the job asks for it, enforced) when the job
  has made no *progress* for ``stall_timeout`` seconds. Progress means a call
  to ``ctx.progress(...)`` (the job decides what counts), not merely output.
* Jobs live in memory. A worker restart loses them (a running child process is
  killed with its parent's process group where the OS allows; otherwise it
  finishes on its own and its output is discarded).

Reusing it
----------
::

    from utils.jobs import JOBS

    def work(ctx):
        ctx.progress(0.1, "downloading")
        rc = run_process(ctx, ["curl", ...], cwd=tmp)       # streams to ctx.log
        if ctx.cancelled: return {"cancelled": True}
        return {"path": out}                                  # becomes job.result

    job = JOBS.start("fab_package", work, params={"board": path}, stall_timeout=120)
    return {"success": True, "async": True, "jobId": job.id, "state": job.state}

    # optional main-thread completion step (e.g. swapping a routed board in):
    job = JOBS.start("autoroute", work, apply_fn=lambda job, params: {...})

``job_status`` / ``job_list`` / ``job_cancel`` / ``job_apply`` are exposed as
MCP tools by ``commands/job_commands.py`` and work for every job kind.
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

logger = logging.getLogger("kicad_interface")

JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled")
FINISHED_STATES = ("succeeded", "failed", "cancelled")

# Finished jobs kept for job_status / job_list before the oldest are dropped.
MAX_FINISHED_JOBS = 50
LOG_LINES_KEPT = 400


class JobCancelled(Exception):
    """Raised by :meth:`JobContext.check_cancelled` inside a cancelled job."""


class Job:
    """State of one background job. Mutated only under ``Job.lock``."""

    def __init__(
        self,
        kind: str,
        params: Optional[Dict[str, Any]] = None,
        stall_timeout: Optional[float] = None,
        apply_fn: Optional[Callable[["Job", Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        self.id = f"{kind}-{uuid.uuid4().hex[:10]}"
        self.kind = kind
        self.params = dict(params or {})
        self.state = "queued"
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.progress: Optional[float] = None
        self.message = ""
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.log: Deque[str] = collections.deque(maxlen=LOG_LINES_KEPT)
        self.last_progress_at = self.created_at
        self.last_output_at = self.created_at
        self.stall_timeout = stall_timeout
        self.apply_fn = apply_fn
        self.applied: Optional[Dict[str, Any]] = None
        self.cancel_event = threading.Event()
        self.processes: List[subprocess.Popen] = []
        self.lock = threading.RLock()
        self.thread: Optional[threading.Thread] = None

    # -- read side -------------------------------------------------------
    def elapsed(self) -> float:
        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return round(end - start, 1)

    def seconds_since_progress(self) -> float:
        return round(time.time() - self.last_progress_at, 1)

    def is_stalled(self) -> bool:
        if self.state != "running" or not self.stall_timeout:
            return False
        return self.seconds_since_progress() > float(self.stall_timeout)

    def snapshot(self, log_lines: int = 20, include_result: bool = True) -> Dict[str, Any]:
        with self.lock:
            out: Dict[str, Any] = {
                "jobId": self.id,
                "kind": self.kind,
                "state": self.state,
                "progress": self.progress,
                "message": self.message,
                "elapsedSeconds": self.elapsed(),
                "secondsSinceProgress": (
                    self.seconds_since_progress() if self.state == "running" else None
                ),
                "stalled": self.is_stalled(),
                "stallTimeout": self.stall_timeout,
                "createdAt": _iso(self.created_at),
                "finishedAt": _iso(self.finished_at) if self.finished_at else None,
                "params": self.params,
            }
            if log_lines:
                out["logTail"] = list(self.log)[-int(log_lines) :]
            if self.error:
                out["error"] = self.error
            if self.apply_fn is not None:
                out["applyStep"] = {
                    "required": True,
                    "applied": self.applied is not None,
                    "hint": (
                        "Call job_apply with this jobId to write the result into the board"
                        if self.applied is None and self.state == "succeeded"
                        else None
                    ),
                }
            if include_result and self.result is not None:
                out["result"] = self.result
            if self.applied is not None:
                out["applyResult"] = self.applied
            return out


class JobContext:
    """Handle given to a job function to report progress and honour cancel."""

    def __init__(self, job: Job) -> None:
        self._job = job

    @property
    def job_id(self) -> str:
        return self._job.id

    @property
    def cancelled(self) -> bool:
        return self._job.cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled()

    def log(self, line: str) -> None:
        with self._job.lock:
            self._job.log.append(str(line).rstrip("\r\n"))
            self._job.last_output_at = time.time()

    def progress(self, fraction: Optional[float] = None, message: Optional[str] = None) -> None:
        """Record progress. This is what resets the stall clock."""
        with self._job.lock:
            if fraction is not None:
                self._job.progress = max(0.0, min(1.0, float(fraction)))
            if message is not None:
                self._job.message = message
            self._job.last_progress_at = time.time()

    def set_message(self, message: str) -> None:
        """Change the status message without counting as progress."""
        with self._job.lock:
            self._job.message = message

    def stalled_for(self) -> float:
        return self._job.seconds_since_progress()

    def register_process(self, proc: subprocess.Popen) -> None:
        with self._job.lock:
            self._job.processes.append(proc)

    def unregister_process(self, proc: subprocess.Popen) -> None:
        with self._job.lock:
            if proc in self._job.processes:
                self._job.processes.remove(proc)


class JobManager:
    """Process-wide registry of jobs."""

    def __init__(self) -> None:
        self._jobs: "collections.OrderedDict[str, Job]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def start(
        self,
        kind: str,
        fn: Callable[[JobContext], Optional[Dict[str, Any]]],
        *,
        params: Optional[Dict[str, Any]] = None,
        stall_timeout: Optional[float] = None,
        apply_fn: Optional[Callable[[Job, Dict[str, Any]], Dict[str, Any]]] = None,
        cleanup_fn: Optional[Callable[[Job], None]] = None,
    ) -> Job:
        """Run ``fn(ctx)`` in a daemon thread and return the job at once.

        ``fn`` returns the job's result dict (or raises). A result containing
        ``"success": False`` marks the job ``failed`` (with the result kept).
        ``apply_fn(job, params)`` is the optional main-thread completion step,
        run by :meth:`apply`. ``cleanup_fn(job)`` runs when the job is
        forgotten (pruned), e.g. to delete a staging directory.
        """
        job = Job(kind, params=params, stall_timeout=stall_timeout, apply_fn=apply_fn)
        job._cleanup_fn = cleanup_fn  # type: ignore[attr-defined]
        ctx = JobContext(job)

        def runner() -> None:
            with job.lock:
                job.state = "running"
                job.started_at = time.time()
                job.last_progress_at = job.started_at
            state, result, error = "succeeded", None, None
            try:
                result = fn(ctx)
                if job.cancel_event.is_set():
                    state = "cancelled"
                elif isinstance(result, dict) and result.get("success") is False:
                    state = "failed"
                    error = result.get("message") or "job reported failure"
            except JobCancelled:
                state = "cancelled"
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(f"Job {job.id} crashed")
                state, error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                _kill_all(job)
            with job.lock:
                job.state = state
                job.result = result
                job.error = error
                job.finished_at = time.time()
                if state == "succeeded":
                    job.progress = 1.0
            if state != "succeeded" and cleanup_fn is not None:
                # Nothing can be applied from a failed/cancelled job: free its files now.
                try:
                    cleanup_fn(job)
                except Exception:
                    pass
            self._prune()

        thread = threading.Thread(target=runner, name=f"job-{job.id}", daemon=True)
        job.thread = thread
        with self._lock:
            self._jobs[job.id] = job
        thread.start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, kind: Optional[str] = None, states: Optional[Sequence[str]] = None) -> List[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        if states:
            jobs = [j for j in jobs if j.state in states]
        return jobs

    def cancel(self, job_id: str) -> Optional[Job]:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel_event.set()
        _kill_all(job)
        return job

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Optional[Job]:
        """Block until the job finishes (used by the synchronous code paths and tests)."""
        job = self.get(job_id)
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
        return job

    def apply(self, job_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run a finished job's completion step on the calling (main) thread."""
        job = self.get(job_id)
        if job is None:
            return {"success": False, "message": f"Unknown job {job_id}"}
        if job.apply_fn is None:
            return {"success": False, "message": f"Job {job_id} ({job.kind}) has no apply step"}
        if job.state != "succeeded":
            return {
                "success": False,
                "message": f"Job {job_id} is {job.state}; only a succeeded job can be applied",
                "state": job.state,
            }
        if job.applied is not None and job.applied.get("success"):
            return {
                "success": False,
                "message": f"Job {job_id} was already applied",
                "applyResult": job.applied,
            }
        result = job.apply_fn(job, dict(params or {}))
        with job.lock:
            # Keep a copy without private dispatcher keys for later status calls.
            job.applied = {k: v for k, v in result.items() if not k.startswith("_")}
        return result

    def _prune(self) -> None:
        with self._lock:
            finished = [j for j in self._jobs.values() if j.state in FINISHED_STATES]
            excess = len(finished) - MAX_FINISHED_JOBS
            for job in finished[: max(0, excess)]:
                self._jobs.pop(job.id, None)
                cleanup = getattr(job, "_cleanup_fn", None)
                if cleanup:
                    try:
                        cleanup(job)
                    except Exception:
                        pass


def _kill_all(job: Job) -> None:
    with job.lock:
        procs = list(job.processes)
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def run_process(
    ctx: JobContext,
    cmd: List[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
    on_line: Optional[Callable[[str], None]] = None,
    log_prefix: str = "",
) -> Dict[str, Any]:
    """Run ``cmd``, streaming its merged stdout/stderr into the job log.

    ``on_line(line)`` sees every output line and is where a caller decides what
    counts as progress (``ctx.progress``). The process is killed when the job
    is cancelled, when ``timeout`` seconds pass, or when ``stall_timeout``
    seconds pass without ``ctx.progress`` being called.

    Returns ``{"returncode", "reason": "exited"|"timeout"|"stalled"|"cancelled",
    "elapsed", "tail": [last lines]}``.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    ctx.register_process(proc)
    tail: Deque[str] = collections.deque(maxlen=60)

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            tail.append(line)
            ctx.log(log_prefix + line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # pragma: no cover - parser bugs must not kill the job
                    pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    start = time.time()
    ctx.progress()  # starting the process is progress
    reason = "exited"
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if ctx.cancelled:
            reason = "cancelled"
        elif timeout and now - start > float(timeout):
            reason = "timeout"
        elif stall_timeout and ctx.stalled_for() > float(stall_timeout):
            reason = "stalled"
        if reason != "exited":
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            break
    t.join(timeout=5)
    ctx.unregister_process(proc)
    if reason == "exited" and ctx.cancelled:
        reason = "cancelled"  # JobManager.cancel killed it before the loop noticed
    return {
        "returncode": proc.returncode,
        "reason": reason,
        "elapsed": round(time.time() - start, 1),
        "tail": list(tail),
    }


JOBS = JobManager()
