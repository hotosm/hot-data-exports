#!/usr/bin/env -S uv run python
"""Resolve scripts/schedule.yaml into oex jobs and run them."""

import argparse
import io
import os
import signal
import sys
from datetime import UTC, date, datetime
from itertools import groupby

import yaml

import due
import jobs
import runner
from schedule import Schedule, select_countries

EPILOG = """examples:
  sweep.py --due --wait                   every due job, after any running sweep
  sweep.py --group normal --only ZWE,VNM  just those countries
  sweep.py --frequency "as needed"        the manual-only jobs

Every line oex logs for a job starts with its id. --due reads each job's last publish from its
published STAC Items.

exit codes:
  1    a job failed or a config file is broken
  2    the schedule or options are invalid, or a last publish can't be read
  3    another sweep holds the lock
  130  the sweep was stopped"""
STOPPED = "sweep: stopped, running jobs were stopped too"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add = parser.add_argument
    add("--group", help="one group from `groups:` in the schedule")
    add("--frequency", help="an HDX frequency name, such as monthly or 'as needed'")
    add(
        "--due",
        action="store_true",
        help="only jobs whose last publish is older than their frequency",
    )
    add("--only", help="comma-separated ISO3 codes to run")
    add("--workers", type=int, help="exports at once, overriding the group's `workers:`")
    add("--timeout", type=int, default=runner.DEFAULT_TIMEOUT_SECONDS, help="per job, in seconds")
    add("--wait", action="store_true", help="wait for the lock; without it a held lock exits 3")
    add("--no-hdx-push", action="store_true", help="skip HDX; files still upload to S3")
    add("--no-resume", action="store_true", help="re-export categories already uploaded")
    add("--dry-run", action="store_true", help="print the jobs, run nothing")
    add("--check", action="store_true", help="load every config, disabled too, run nothing")
    args = parser.parse_args()
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def report_broken(broken: list[str]) -> None:
    for line in broken:
        print(f"sweep: broken {line}", file=sys.stderr)


def check(schedule: dict, group: str | None) -> int:
    everything, _, broken = Schedule(schedule, date.today(), every=True).resolve(group)
    broken += due.unloadable(everything)
    report_broken(broken)
    print(f"sweep: checked {len(everything)} job(s), {len(broken)} broken")
    return 1 if broken else 0


def run(selected: list[jobs.Job], timeout: int) -> list[str]:
    job_runner = runner.Runner(timeout)
    failures: list[str] = []
    for _, grouped in groupby(selected, key=lambda job: job.group):
        batch = list(grouped)
        failures += job_runner.run(batch, batch[0].workers)
    return failures


def main() -> int:
    # stdout is block-buffered in a pipe, so a run waiting for the lock would log nothing.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    # A container or pod stops with SIGTERM; the Ctrl+C path then stops every running job.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    # oex reads a config's relative paths, such as categories_file, from the working directory.
    os.chdir(jobs.REPO_ROOT)
    args = parse_args()
    settings: dict[str, object] = {}
    if args.no_hdx_push:
        settings["hdx.push"] = False
    if args.no_resume:
        settings["output.resume"] = False
    schedule = yaml.safe_load(jobs.SCHEDULE_FILE.read_text(encoding="utf-8")) or {}
    try:
        if args.check:
            return check(schedule, args.group)
        # Decided under the lock, so a waiting sweep never re-exports what the holder just did.
        lock = None if args.dry_run else runner.acquire_lock(args.wait)
        if lock is None and not args.dry_run:
            print("sweep: another sweep holds the lock, refusing to overlap", file=sys.stderr)
            return 3
        plan = Schedule(schedule, date.today(), args.frequency, settings, args.workers)
        selected, skipped, broken = plan.resolve(args.group)
        if args.due:
            selected, unloadable = due.due_jobs(selected, datetime.now(UTC))
            broken += unloadable
        if args.only:
            selected = select_countries(selected, args.only)
    except jobs.ScheduleError as error:
        print(f"sweep: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(STOPPED, file=sys.stderr)
        return 130

    report_broken(broken)
    broken_code = 1 if broken else 0
    for line in skipped:
        print(f"skip {line}")
    print(f"sweep: {len(selected)} job(s)")
    if args.dry_run:
        for job in selected:
            print(f"{job.id}: {job.command} {job.config} {job.overrides()}")
        return broken_code
    if not selected:
        return broken_code
    try:
        failures = run(selected, args.timeout)
    except KeyboardInterrupt:
        print(STOPPED, file=sys.stderr)
        return 130
    if failures:
        print(
            f"sweep: {len(failures)}/{len(selected)} failed: {', '.join(failures)}", file=sys.stderr
        )
        return 1
    print(f"sweep: complete {len(selected)}/{len(selected)}")
    return broken_code


if __name__ == "__main__":
    sys.exit(main())
