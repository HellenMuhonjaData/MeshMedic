"""Background job progress (Phase 2, STORY-015). Job progress only -- never clinical content.

The clinical record stays the audit trail. This store only tracks where each
encounter's background processing is, so it can resume after a restart,
retry safely, and never process the same encounter twice at once.

What a job row holds: ids, stage, status, attempt count, next retry time,
lease owner/expiry, error type and timestamps. No names, MRNs, dates of
birth, transcripts or note text (tests scan every column for that).

`JobStore` is the interface the worker uses. `SqliteJobStore` is the
single-machine implementation; a dedicated job service can replace it later
by implementing the same methods -- nothing outside this module knows SQLite
is involved.

Statuses: queued -> running -> ready (done) | retry_wait -> running ...
          | needs_attention (gave up after MAX_ATTEMPTS, or a non-retryable error)
"""

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

STAGES = ("transcribe", "draft", "record", "check")
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (15, 60, 240)
LEASE_SECONDS = 300


@dataclass(frozen=True)
class Job:
    job_id: str
    encounter_id: str
    stage: str
    status: str
    attempts: int
    next_attempt_at: float
    error_class: str | None
    created_at: float
    updated_at: float


class JobStore(Protocol):
    def enqueue(self, encounter_id: str) -> Job: ...
    def claim(self, worker_id: str) -> Job | None: ...
    def heartbeat(self, job_id: str, worker_id: str) -> None: ...
    def advance(self, job_id: str, next_stage: str) -> None: ...
    def finish(self, job_id: str) -> None: ...
    def fail(self, job_id: str, error_class: str, retryable: bool) -> Job: ...
    def retry_now(self, encounter_id: str) -> Job | None: ...
    def get_for_encounter(self, encounter_id: str) -> Job | None: ...
    def all_jobs(self) -> list[Job]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    lease_owner TEXT,
    lease_expires_at REAL,
    error_class TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""
_COLUMNS = "job_id, encounter_id, stage, status, attempts, next_attempt_at, error_class, created_at, updated_at"


class SqliteJobStore:
    def __init__(self, path: Path | str, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)

    def _row(self, row) -> Job | None:
        return Job(*row) if row else None

    def _get(self, job_id: str) -> Job | None:
        return self._row(self._db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def enqueue(self, encounter_id: str) -> Job:
        """Idempotent: one job per encounter, however many times this is called."""
        now = self._clock()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO jobs (job_id, encounter_id, stage, status, attempts, next_attempt_at,"
                " created_at, updated_at) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?)",
                (str(uuid.uuid4()), encounter_id, STAGES[0], now, now, now),
            )
            return self.get_for_encounter(encounter_id)

    def claim(self, worker_id: str) -> Job | None:
        """Take the next due job: queued, waiting to retry, or running with an expired lease (a crashed worker)."""
        now = self._clock()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    f"SELECT {_COLUMNS} FROM jobs WHERE (status IN ('queued','retry_wait') AND next_attempt_at <= ?)"
                    " OR (status='running' AND lease_expires_at < ?) ORDER BY created_at LIMIT 1",
                    (now, now),
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                self._db.execute(
                    "UPDATE jobs SET status='running', lease_owner=?, lease_expires_at=?, updated_at=? WHERE job_id=?",
                    (worker_id, now + LEASE_SECONDS, now, row[0]),
                )
                self._db.execute("COMMIT")
            except sqlite3.Error:
                self._db.execute("ROLLBACK")
                raise
            return self._get(row[0])

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        now = self._clock()
        with self._lock:
            self._db.execute("UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE job_id=? AND lease_owner=?",
                             (now + LEASE_SECONDS, now, job_id, worker_id))

    def advance(self, job_id: str, next_stage: str) -> None:
        if next_stage not in STAGES:
            raise ValueError(f"unknown stage {next_stage!r}")
        with self._lock:
            self._db.execute("UPDATE jobs SET stage=?, updated_at=? WHERE job_id=?", (next_stage, self._clock(), job_id))

    def finish(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET status='ready', lease_owner=NULL, error_class=NULL, updated_at=?"
                             " WHERE job_id=?", (self._clock(), job_id))

    def fail(self, job_id: str, error_class: str, retryable: bool) -> Job:
        """Record a failed attempt. Retry with backoff up to MAX_ATTEMPTS, then needs_attention."""
        now = self._clock()
        with self._lock:
            job = self._get(job_id)
            attempts = job.attempts + 1
            if retryable and attempts < MAX_ATTEMPTS:
                status, next_at = "retry_wait", now + BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
            else:
                status, next_at = "needs_attention", now
            self._db.execute(
                "UPDATE jobs SET status=?, attempts=?, next_attempt_at=?, error_class=?, lease_owner=NULL,"
                " updated_at=? WHERE job_id=?",
                (status, attempts, next_at, error_class, now, job_id),
            )
            return self._get(job_id)

    def retry_now(self, encounter_id: str) -> Job | None:
        """A clinician pressed Retry on an encounter that needs attention."""
        now = self._clock()
        with self._lock:
            self._db.execute("UPDATE jobs SET status='queued', attempts=0, next_attempt_at=?, error_class=NULL,"
                             " updated_at=? WHERE encounter_id=? AND status='needs_attention'",
                             (now, now, encounter_id))
            return self.get_for_encounter(encounter_id)

    def get_for_encounter(self, encounter_id: str) -> Job | None:
        return self._row(self._db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE encounter_id=?",
                                          (encounter_id,)).fetchone())

    def all_jobs(self) -> list[Job]:
        return [Job(*r) for r in self._db.execute(f"SELECT {_COLUMNS} FROM jobs ORDER BY created_at").fetchall()]

    def raw_rows(self) -> list[tuple]:
        """Every stored value, for the no-patient-data test."""
        return self._db.execute("SELECT * FROM jobs").fetchall()

    def close(self) -> None:
        self._db.close()
