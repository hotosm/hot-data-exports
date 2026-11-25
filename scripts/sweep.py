#!/usr/bin/env -S uv run python
"""Resolve scripts/schedule.yaml into oex-cli jobs and run them."""

import argparse
import sys
from datetime import UTC, date, datetime
from itertools import groupby

import yaml

import due
import jobs
import runner
from schedule import Schedule, select_countries

EPILOG = """examples:
  sweep.py                                        every enabled job
  sweep.py --group priority --frequency monthly   one group at one frequency
  sweep.py --frequency "as needed"                the manual-only jobs
  sweep.py --dry-run                              print the commands, run nothing
  sweep.py --group priority --no-resume           re-export even if state says done
  sweep.py --group normal --only ZWE,VNM          just those countries
  sweep.py --group normal --workers 4             four countries at once
  sweep.py --due                                  only jobs whose last publish is older
                                                  than their frequency

With more than one worker each job's output goes to .sweep/logs/<job-id>.log. --due reads
each job's last publish from its published STAC Items.

exit codes: 1 a job failed or a config file is broken, 2 the schedule is malformed, 3 another
sweep holds the lock."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--group", help="one group from `groups:` in the schedule")
    parser.add_argument("--frequency", help="an HDX frequency name, such as monthly or 'as needed'")
    parser.add_argument(
        "--timeout",
        type=int,
        default=runner.DEFAULT_TIMEOUT_SECONDS,
        help=f"per job, seconds (default {runner.DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    parser.add_argument(
        "--check",
        action="store_true",
        help="load every config as oex would, including disabled ones, and run nothing",
    )
    parser.add_argument(
        "--no-hdx-push",
        action="store_true",
        help="rehearse against the real configs without publishing to HDX",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="exports at once in each country group, overriding the group's `workers:`",
    )
    parser.add_argument(
        "--due",
        action="store_true",
        help="only jobs whose last publish is older than their frequency",
    )
    parser.add_argument(
        "--only", help="comma-separated ISO3 codes; run just these, for resuming a long sweep"
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="re-export every category even if the state file says it is already uploaded",
    )
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
    """Each group's jobs in turn; a closing job that succeeds marks its event closed."""
    job_runner = runner.Runner(timeout)
    failures: list[str] = []
    for _, grouped in groupby(selected, key=lambda job: job.group):
        batch = list(grouped)
        failures += job_runner.run(batch, batch[0].workers)
    for job in selected:
        if job.closes is not None and job.id not in failures:
            job.closes.parent.mkdir(parents=True, exist_ok=True)
            job.closes.touch()
    return failures


def main() -> int:
    args = parse_args()
    extra = ("--no-hdx-push",) if args.no_hdx_push else ()
    if args.no_resume:
        extra += ("--no-resume",)
    schedule = yaml.safe_load(jobs.SCHEDULE_FILE.read_text(encoding="utf-8")) or {}
    try:
        if args.check:
            return check(schedule, args.group)
        plan = Schedule(schedule, date.today(), args.frequency, extra, args.workers)
        selected, skipped, broken = plan.resolve(args.group)
        if args.due:
            selected, unloadable = due.due_jobs(selected, datetime.now(UTC))
            broken += unloadable
        if args.only:
            selected = select_countries(selected, args.only)
    except jobs.ScheduleError as error:
        print(f"sweep: {error}", file=sys.stderr)
        return 2

    report_broken(broken)
    broken_code = 1 if broken else 0
    for line in skipped:
        print(f"skip {line}")
    print(f"sweep: {len(selected)} job(s)")
    if args.dry_run:
        for job in selected:
            print(" ".join(job.argv()))
        return broken_code
    if not selected:
        return broken_code
    lock = runner.acquire_lock()
    if lock is None:
        print("sweep: another sweep holds the lock, refusing to overlap", file=sys.stderr)
        return 3
    failures = run(selected, args.timeout)
    if failures:
        print(
            f"sweep: {len(failures)}/{len(selected)} failed: {', '.join(failures)}", file=sys.stderr
        )
        return 1
    print(f"sweep: complete {len(selected)}/{len(selected)}")
    return broken_code


if __name__ == "__main__":
    sys.exit(main())
