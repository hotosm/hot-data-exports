from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from oex.system import adaptive_parallel_resources
from omegaconf import OmegaConf
from omegaconf.errors import OmegaConfBaseException

import jobs
from events import closing_job, merge_event
from jobs import (
    MANUAL_FREQUENCY,
    Job,
    ScheduleError,
    commands_for,
    expiry_date,
    frequency_days,
)


@dataclass(frozen=True)
class Candidate:
    """One country or config file a group names; `problem` marks a file that can't be read."""

    label: str
    attributes: dict
    config: Path | None = None
    problem: str | None = None


def normalise_attributes(value: object, default_frequency: str | None) -> dict:
    """A schedule value, either a bare frequency or a mapping, as a mapping with defaults."""
    if value is None:
        attributes: dict = {}
    elif isinstance(value, str):
        attributes = {"frequency": value}
    elif isinstance(value, dict):
        attributes = dict(value)
    else:
        raise ScheduleError(f"expected a frequency or a mapping, got {value!r}")
    attributes.setdefault("frequency", default_frequency)
    attributes.setdefault("enabled", True)
    return attributes


def skip_reason(attributes: dict, frequency_filter: str | None, today: date) -> str | None:
    if not attributes["enabled"]:
        return "disabled"
    expires = attributes.get("expires")
    if expires is not None and today > expiry_date(expires):
        return f"expired {expires}"
    if frequency_filter is None and attributes["frequency"] == MANUAL_FREQUENCY:
        return f'manual, run it with --frequency "{MANUAL_FREQUENCY}"'
    return None


def memory_gb_per_worker(workers: int) -> int:
    """oex's adaptive memory split between concurrent exports, which would each claim it all."""
    _, memory_gb = adaptive_parallel_resources()
    return max(1, memory_gb // workers)


def country_config(iso3: str, frequency: str, memory_gb: int | None = None) -> Path:
    """The country's override over base, with the schedule's frequency, which HDX shows."""
    layers = [OmegaConf.load(jobs.BASE_CONFIG)]
    override = jobs.COUNTRY_CONFIG_DIR / f"{iso3}.yaml"
    if override.exists():
        layers.append(OmegaConf.load(override))
    layers.append(OmegaConf.create({"frequency": frequency}))
    if memory_gb is not None:
        layers.append(OmegaConf.create({"parallel": {"memory_gb": memory_gb}}))
    return jobs.write_merged(f"{iso3}.yaml", OmegaConf.merge(*layers))


def folder_candidates(name: str, group: dict) -> list[Candidate]:
    folder = jobs.REPO_ROOT / group["dir"]
    if not folder.is_dir():
        raise ScheduleError(f"group {name!r}: {folder} is not a directory")
    overrides = group.get("overrides") or {}
    candidates = []
    # A leading underscore marks a partial included by another config, not a job.
    for config in sorted(folder.glob("[!_]*.yaml")):
        problem = None
        try:
            raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, ValueError) as error:
            raw, problem = None, f"not valid YAML: {error}"
        if not isinstance(raw, dict):
            raw, problem = {}, problem or "not a YAML mapping of settings"
        attributes = normalise_attributes(
            overrides.get(config.name), raw.get("frequency") or group.get("frequency")
        )
        if "expires" in raw:
            attributes.setdefault("expires", raw["expires"])
        candidates.append(Candidate(config.stem, attributes, config, problem))
    return candidates


def group_candidates(name: str, group: dict) -> list[Candidate]:
    if "countries" in group:
        members = group["countries"] or {}
        if isinstance(members, list):
            members = dict.fromkeys(members)
        return [
            Candidate(iso3, normalise_attributes(value, group.get("frequency")))
            for iso3, value in members.items()
        ]
    if "dir" in group:
        return folder_candidates(name, group)
    raise ScheduleError(f"group {name!r} has neither `countries:` nor `dir:`")


def group_workers(name: str, group: dict, workers_override: int | None) -> int:
    """Concurrent exports need a memory budget each, which only country configs can take."""
    if "dir" in group:
        if group.get("workers", 1) != 1:
            raise ScheduleError(f"group {name!r}: a folder of configs runs one job at a time")
        return 1
    workers = workers_override or group.get("workers", 1)
    if workers < 1:
        raise ScheduleError(f"group {name!r}: workers must be at least 1")
    return workers


def select_countries(selected: list[Job], only: str) -> list[Job]:
    wanted = {code.strip().upper() for code in only.split(",") if code.strip()}
    unknown = wanted - {job.iso3 for job in selected if job.iso3}
    if unknown:
        raise ScheduleError(f"--only names no job for {sorted(unknown)}")
    return [job for job in selected if job.iso3 in wanted]


class Schedule:
    """The schedule's jobs for one sweep; `every` also keeps disabled, expired and manual ones."""

    def __init__(
        self,
        schedule: dict,
        today: date,
        frequency: str | None = None,
        settings: dict[str, object] | None = None,
        workers: int | None = None,
        every: bool = False,
    ):
        groups = schedule.get("groups")
        if not groups:
            raise ScheduleError("schedule has no `groups:` list")
        self.groups: list[str] = list(groups)
        self.schedule = schedule
        self.today = today
        self.frequency = frequency
        self.settings = settings or {}
        self.workers = workers
        self.every = every

    def resolve(self, only_group: str | None = None) -> tuple[list[Job], list[str], list[str]]:
        """Jobs to run, skip lines, and broken lines for folder configs that cannot run."""
        if only_group is not None and only_group not in self.groups:
            known = ", ".join(self.groups)
            raise ScheduleError(f"unknown group {only_group!r}, known: {known}")
        resolved: list[Job] = []
        skipped: list[str] = []
        broken: list[str] = []
        for name in self.groups:
            if only_group is not None and name != only_group:
                continue
            group = self.schedule.get(name)
            if group is None:
                raise ScheduleError(f"group {name!r} is listed in `groups:` but has no block")
            if not group.get("enabled", True) and not self.every:
                skipped.append(f"{name}: group disabled")
                continue
            if "base" in group and not (jobs.REPO_ROOT / group["base"]).is_file():
                raise ScheduleError(f"group {name!r}: base {group['base']} does not exist")
            workers = group_workers(name, group, self.workers)
            memory_gb = memory_gb_per_worker(workers) if workers > 1 else None
            for candidate in group_candidates(name, group):
                label = f"{name}/{candidate.label}"
                try:
                    made, reason = self._jobs(name, group, candidate, workers, memory_gb)
                except (ScheduleError, yaml.YAMLError, OmegaConfBaseException) as error:
                    # One broken file in a folder must not stop every other export.
                    if candidate.config is None:
                        raise ScheduleError(f"{label}: {error}") from error
                    broken.append(f"{label}: {error}")
                    continue
                resolved += made
                if reason is not None:
                    skipped.append(f"{label}: {reason}")
        return resolved, skipped, broken

    def _jobs(
        self, name: str, group: dict, candidate: Candidate, workers: int, memory_gb: int | None
    ) -> tuple[list[Job], str | None]:
        attributes = candidate.attributes
        if candidate.problem is not None:
            raise ScheduleError(candidate.problem)
        if attributes["frequency"] is None:
            raise ScheduleError("no frequency here or on the group")
        if frequency_days(attributes["frequency"]) is None:
            raise ScheduleError(f"{attributes['frequency']!r} is not an HDX frequency name")
        if self.frequency is not None and attributes["frequency"] != self.frequency:
            return [], None
        if self.every:
            if attributes.get("expires") is not None:
                expiry_date(attributes["expires"])
        elif reason := skip_reason(attributes, self.frequency, self.today):
            return self._closing(name, group, candidate, reason)

        if candidate.config is None:
            config = country_config(candidate.label, attributes["frequency"], memory_gb)
        elif "base" in group:
            merged = merge_event(candidate.config, jobs.REPO_ROOT / group["base"])
            # oex publishes the frequency to HDX; without one it would advertise its own default.
            merged.setdefault("frequency", group.get("frequency"))
            config = jobs.write_merged(f"{name}/{candidate.config.name}", merged)
        else:
            config = candidate.config
        iso3 = candidate.label if candidate.config is None else None
        commands = commands_for(config)
        return [
            Job(
                f"{name}/{candidate.label}" + (f":{command}" if len(commands) > 1 else ""),
                name,
                command,
                config,
                iso3,
                self.settings,
                attributes["frequency"],
                workers,
            )
            for command in commands
        ], None

    def _closing(
        self, name: str, group: dict, candidate: Candidate, reason: str
    ) -> tuple[list[Job], str]:
        closes = (
            reason.startswith("expired")
            and "base" in group
            and self.frequency is None
            and self.settings.get("hdx.push") is not False
        )
        if not closes or candidate.config is None:
            return [], reason
        base = jobs.REPO_ROOT / group["base"]
        closing = closing_job(name, base, candidate.config, candidate.attributes["expires"])
        if isinstance(closing, Job):
            return [closing], reason
        return [], f"{reason}, {closing}"
