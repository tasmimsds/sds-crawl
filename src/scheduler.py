"""Lightweight in-process auto-sync scheduler (APScheduler).

Schedules are persisted in the `schedules` table and reloaded on app start.
A scheduled run = a full sync (crawl + fact rules + technical), same as clicking
"Sync Now", so the dashboard's issue movement stays current automatically.
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import connect, now_iso
from .jobs import create_job, run_sync_job

_scheduler: AsyncIOScheduler | None = None
DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


async def _run_scheduled_sync(source_id: int) -> None:
    conn = connect()
    # concurrency guard: skip if a sync is already running/queued for this site
    if conn.execute(
        "SELECT id FROM jobs WHERE source_id=? AND status IN ('running','queued')",
        (source_id,),
    ).fetchone():
        return
    job_id = create_job(conn, "sync", source_id)
    # scheduled runs also refresh external/web findings
    await run_sync_job(conn, job_id, source_id, only_changed=False, include_external=True)


def _trigger_for(row) -> CronTrigger | None:
    if not row or row["mode"] == "off" or not row["enabled"]:
        return None
    if row["mode"] == "daily":
        return CronTrigger(hour=row["hour"], minute=row["minute"])
    if row["mode"] == "weekly":
        return CronTrigger(day_of_week=DOW[row["day_of_week"] % 7],
                           hour=row["hour"], minute=row["minute"])
    return None


def reschedule(source_id: int) -> str | None:
    """(Re)install the APScheduler job for one source; return next-run ISO or None."""
    conn = connect()
    sched = get_scheduler()
    job_id = f"sync-{source_id}"
    if sched.get_job(job_id):
        sched.remove_job(job_id)
    row = conn.execute("SELECT * FROM schedules WHERE source_id=?", (source_id,)).fetchone()
    trig = _trigger_for(row)
    next_run = None
    if trig is not None:
        job = sched.add_job(_run_scheduled_sync, trig, args=[source_id], id=job_id,
                            replace_existing=True, misfire_grace_time=3600)
        next_run = job.next_run_time.isoformat() if job.next_run_time else None
    conn.execute("UPDATE schedules SET next_run=?, updated_at=? WHERE source_id=?",
                 (next_run, now_iso(), source_id))
    conn.commit()
    return next_run


def start_and_load() -> None:
    """Start the scheduler and (re)install jobs for all saved schedules."""
    conn = connect()
    sched = get_scheduler()
    if not sched.running:
        sched.start()
    for row in conn.execute("SELECT source_id FROM schedules WHERE mode!='off' AND enabled=1"):
        reschedule(row["source_id"])


def set_schedule(source_id: int, mode: str, day_of_week: int, hour: int, minute: int) -> str | None:
    conn = connect()
    conn.execute(
        """INSERT INTO schedules (source_id, mode, day_of_week, hour, minute, enabled, updated_at)
           VALUES (?,?,?,?,?,1,?)
           ON CONFLICT(source_id) DO UPDATE SET
             mode=excluded.mode, day_of_week=excluded.day_of_week, hour=excluded.hour,
             minute=excluded.minute, enabled=1, updated_at=excluded.updated_at""",
        (source_id, mode, day_of_week, hour, minute, now_iso()),
    )
    conn.commit()
    return reschedule(source_id)
