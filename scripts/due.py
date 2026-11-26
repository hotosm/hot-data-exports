from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError
from oex.catalog import S3CatalogStore
from oex.config.loader import ConfigError, load_config
from oex.stac import last_published, writes_items
from omegaconf.errors import OmegaConfBaseException

from jobs import Job, ScheduleError, frequency_days

CATALOG_READERS = 16


def last_publish(job: Job) -> datetime | None:
    """When the job's layers last all reached HDX; None makes the job due."""
    config = load_config(job.config, {"iso3": job.iso3} if job.iso3 else None)
    if not writes_items(config):
        return None
    return last_published(config, job.command, S3CatalogStore(config))


def is_due(job: Job, last: datetime | None, now: datetime) -> bool:
    """Due once the frequency's calendar days have passed, so a late finish counts that day."""
    days = frequency_days(job.frequency)
    if days is None:
        raise ScheduleError(f"{job.id}: {job.frequency!r} is not an HDX frequency name")
    if days <= 0:
        return False
    return last is None or (now.date() - last.date()).days >= days


def due_jobs(candidates: list[Job], now: datetime) -> tuple[list[Job], list[str]]:
    """Due jobs, and a line per folder config oex cannot load, which is left out of the run."""

    def published(job: Job) -> tuple[datetime | None, str | None]:
        if job.closes is not None:
            return None, None
        try:
            return last_publish(job), None
        except (ConfigError, OmegaConfBaseException) as error:
            if job.iso3 is None:
                return None, f"{job.id}: {error}"
            raise ScheduleError(f"{job.id}: cannot read its last publish: {error}") from error
        except (BotoCoreError, ClientError) as error:
            raise ScheduleError(f"{job.id}: cannot read its last publish: {error}") from error

    with ThreadPoolExecutor(max_workers=CATALOG_READERS) as pool:
        results = list(pool.map(published, candidates))
    broken = [problem for _, problem in results if problem is not None]
    due = [
        job
        for job, (moment, problem) in zip(candidates, results, strict=True)
        if problem is None and (job.closes is not None or is_due(job, moment, now))
    ]
    return due, broken


def unloadable(candidates: list[Job]) -> list[str]:
    problems = []
    for config, iso3, job_id in {
        (job.config, job.iso3, job.id.split(":")[0]) for job in candidates
    }:
        try:
            load_config(config, {"iso3": iso3} if iso3 else None)
        except (ConfigError, OmegaConfBaseException) as error:
            problems.append(f"{job_id}: {error}")
    return sorted(problems)
