"""
core.jobs — background admin jobs (currently `sync` and `ingest`).

Reuses the same UX shape as the export task store: each job persists to a
markdown file under `exports/jobs/<id>.md` (frontmatter for status/metadata,
body for streamed log output) so the wizard's `Manage › Sync & Ingest`
panel can show running progress and a final result without depending on
in-memory state.

Each job spawns `python booki <kind> <args…>` as a subprocess. stdout +
stderr stream into the job's log line-by-line. On completion the job ends
in `success` (exit 0) or `failed` (non-zero / exception).

Arguments are sanitized against a per-kind allowlist before they're passed
to the subprocess — the UI cannot inject arbitrary shell.
"""

from __future__ import annotations

import logging
import queue
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

# Reuse YAML helpers + frontmatter regex + ISO timestamp helper from the
# exporter task store so we don't duplicate them.
from .exporter import (
    TASK_FRONTMATTER_RE,
    _now_iso,
    _yaml_dump_flat,
    _yaml_load_flat,
)

log = logging.getLogger("booki.jobs")

# ─── allowlist of CLI flags per job kind ────────────────────────────────────

# Keys are flag names; values are "bool" (no value) or "list" (one or more
# values, terminated by the next flag). Anything not in the table is rejected.
SYNC_FLAGS: dict[str, str] = {
    "--source": "list",
    "--enrich": "bool",
    "--enrich-meta": "bool",
    "--enricher": "list",
    "--check-dead-links": "bool",
    "--all": "bool",
    "--no-sync": "bool",
    "--dry-run": "bool",
}
INGEST_FLAGS: dict[str, str] = {"--reset": "bool"}
JOB_FLAGS: dict[str, dict[str, str]] = {"sync": SYNC_FLAGS, "ingest": INGEST_FLAGS}
JOB_KINDS = set(JOB_FLAGS)

# Values must be plain words / paths (no spaces, quotes, shell metacharacters).
_SAFE_VALUE_RE = re.compile(r"^[\w][\w\-./]*$")


def _sanitize_args(kind: str, args: list[str]) -> list[str]:
    table = JOB_FLAGS.get(kind, {})
    out: list[str] = []
    i = 0
    while i < len(args):
        a = str(args[i])
        spec = table.get(a)
        if spec is None:
            raise ValueError(f"unknown flag for {kind}: {a}")
        out.append(a)
        i += 1
        if spec == "list":
            consumed = 0
            while i < len(args) and not str(args[i]).startswith("--"):
                v = str(args[i])
                if not _SAFE_VALUE_RE.match(v):
                    raise ValueError(f"unsafe value for {a}: {v!r}")
                out.append(v)
                i += 1
                consumed += 1
            if consumed == 0:
                raise ValueError(f"{a} expects at least one value")
    return out


# ─── persistence ────────────────────────────────────────────────────────────

@dataclass
class Job:
    id: str
    kind: str
    args: list[str] = field(default_factory=list)
    status: str = "pending"            # pending | running | success | failed
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    exit_code: Optional[int] = None
    error: str = ""
    log: str = ""

    def to_frontmatter(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "args": list(self.args),
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
        }

    def to_api(self) -> dict:
        d = self.to_frontmatter()
        d["log"] = self.log
        return d


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class JobStore:
    """File-backed store under `<exports_root>/jobs/`. Thread-safe."""

    def __init__(self, dir_: Path):
        self.dir = dir_
        self.dir.mkdir(parents=True, exist_ok=True)
        self._dir_resolved = self.dir.resolve()
        self._lock = threading.Lock()

    def _path(self, jid: str) -> Path:
        # Same path-traversal guard as TaskStore (P1-01). `jid` reaches us
        # from /api/jobs/{jid} unvalidated; without the regex + relative_to
        # check, a jid like `../../etc/hosts` lets `_read` open arbitrary
        # `.md` files and `delete` unlink them.
        if not _JOB_ID_RE.match(jid or ""):
            return self.dir / f"{uuid.uuid4().hex}__invalid.md"
        p = self.dir / f"{jid}.md"
        try:
            p.resolve().relative_to(self._dir_resolved)
        except (ValueError, OSError):
            return self.dir / f"{uuid.uuid4().hex}__invalid.md"
        return p

    def list(self) -> list[Job]:
        out: list[Job] = []
        for p in sorted(self.dir.glob("*.md")):
            try:
                j = self._read(p)
                if j is not None:
                    out.append(j)
            except Exception:
                log.exception("job_read_failed", extra={"path": str(p)})
        out.sort(key=lambda j: j.created_at, reverse=True)
        return out

    def get(self, jid: str) -> Optional[Job]:
        p = self._path(jid)
        if not p.exists():
            return None
        return self._read(p)

    def _read(self, p: Path) -> Optional[Job]:
        text = p.read_text(encoding="utf-8")
        m = TASK_FRONTMATTER_RE.match(text)
        if not m:
            return None
        fm = _yaml_load_flat(m.group(1))
        body = text[m.end():]
        return Job(
            id=str(fm.get("id") or p.stem),
            kind=str(fm.get("kind") or ""),
            args=list(fm.get("args") or []),
            status=str(fm.get("status") or "pending"),
            created_at=str(fm.get("created_at") or ""),
            started_at=str(fm.get("started_at") or ""),
            finished_at=str(fm.get("finished_at") or ""),
            exit_code=fm.get("exit_code"),
            error=str(fm.get("error") or ""),
            log=body,
        )

    def write(self, j: Job) -> None:
        with self._lock:
            self._write_unlocked(j)

    def _write_unlocked(self, j: Job) -> None:
        content = "---\n" + _yaml_dump_flat(j.to_frontmatter()) + "\n---\n" + (j.log or "")
        self._path(j.id).write_text(content, encoding="utf-8")

    def append_log(self, jid: str, chunk: str) -> None:
        with self._lock:
            j = self.get(jid)
            if j is None:
                return
            j.log = (j.log or "") + chunk
            self._write_unlocked(j)

    def delete(self, jid: str) -> bool:
        with self._lock:
            p = self._path(jid)
            if not p.exists():
                return False
            p.unlink()
            return True


# ─── runner ─────────────────────────────────────────────────────────────────

class JobRunner:
    """
    Single worker thread, serial queue. Submitting a job appends its id to
    the queue; the worker drains the queue and runs each job's subprocess
    sequentially. Output streams to the job's log as it arrives.
    """

    def __init__(self, store: JobStore, project_root: Path):
        self.store = store
        self.root = project_root
        self._q: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._cur_proc: Optional[subprocess.Popen] = None
        self._cur_id: Optional[str] = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._worker.start()
        # Recovery: any job left running/pending across a restart is marked
        # failed (we can't safely resume an interrupted CLI invocation).
        for j in self.store.list():
            if j.status in ("pending", "running"):
                j.status = "failed"
                j.error = "Server restarted before job completed"
                j.finished_at = _now_iso()
                self.store.write(j)
                self.store.append_log(j.id, "[recovery] marked failed after server restart\n")

    def submit(self, kind: str, args: list[str]) -> Job:
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        clean = _sanitize_args(kind, list(args))
        jid = uuid.uuid4().hex[:12]
        j = Job(id=jid, kind=kind, args=clean,
                status="pending", created_at=_now_iso())
        self.store.write(j)
        self._q.put(jid)
        return j

    def cancel(self, jid: str) -> bool:
        if self._cur_id == jid and self._cur_proc and self._cur_proc.poll() is None:
            try:
                self._cur_proc.terminate()
                return True
            except Exception:
                return False
        return False

    def _loop(self) -> None:
        while self._running:
            try:
                jid = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_one(jid)
            except Exception:
                log.exception("job_worker_crash", extra={"job_id": jid})

    def _run_one(self, jid: str) -> None:
        j = self.store.get(jid)
        if j is None:
            return

        j.status = "running"
        j.started_at = _now_iso()
        j.error = ""
        self.store.write(j)
        cmd = [sys.executable, str(self.root / "booki"), j.kind, *j.args]
        self.store.append_log(jid, f"$ {' '.join(cmd[1:])}\n")

        try:
            self._cur_proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._cur_id = jid
            assert self._cur_proc.stdout is not None
            for line in self._cur_proc.stdout:
                self.store.append_log(jid, line)
            rc = self._cur_proc.wait()
            j = self.store.get(jid) or j
            j.exit_code = rc
            j.status = "success" if rc == 0 else "failed"
            if rc != 0:
                j.error = f"exit code {rc}"
            j.finished_at = _now_iso()
            self.store.write(j)
        except Exception as e:
            log.exception("job_failed", extra={"job_id": jid, "kind": j.kind})
            j = self.store.get(jid) or j
            j.status = "failed"
            j.error = f"{type(e).__name__}: {e}"
            j.finished_at = _now_iso()
            self.store.write(j)
        finally:
            self._cur_proc = None
            self._cur_id = None


# ─── FastAPI wiring ─────────────────────────────────────────────────────────

class JobRunRequest(BaseModel):
    # Bound the request shape so the validator can't be hit with a
    # multi-MB args list before _sanitize_args runs. (P1-06)
    kind: str = Field(min_length=1, max_length=32)
    args: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("args")
    @classmethod
    def _cap_arg_strings(cls, v: list[str]) -> list[str]:
        return [str(x)[:512] for x in v]


def attach_routes(app: FastAPI, exports_root: Path, project_root: Path) -> None:
    """Wire all /api/jobs* routes onto `app`."""
    jobs_dir = exports_root / "jobs"
    store = JobStore(jobs_dir)
    runner = JobRunner(store, project_root)
    runner.start()

    @app.post("/api/jobs/run")
    def run_job(req: JobRunRequest):
        if req.kind not in JOB_KINDS:
            raise HTTPException(400, f"Unknown job kind: {req.kind}")
        try:
            j = runner.submit(req.kind, req.args)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"job_id": j.id, "status": j.status}

    @app.get("/api/jobs")
    def list_jobs():
        return [j.to_api() for j in store.list()]

    @app.get("/api/jobs/{jid}")
    def get_job(jid: str):
        j = store.get(jid)
        if j is None:
            raise HTTPException(404, f"Job not found: {jid}")
        return j.to_api()

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: str):
        ok = runner.cancel(jid)
        return {"cancelled": ok}

    @app.delete("/api/jobs/{jid}")
    def delete_job(jid: str):
        ok = store.delete(jid)
        if not ok:
            raise HTTPException(404, f"Job not found: {jid}")
        return {"deleted": jid}

    @app.get("/api/jobs/_meta")
    def jobs_meta():
        """Schema the wizard uses to render flag toggles per kind."""
        return {kind: dict(flags) for kind, flags in JOB_FLAGS.items()}
