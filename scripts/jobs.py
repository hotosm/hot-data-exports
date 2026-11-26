import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from hdx.data.dataset import Dataset
from omegaconf import DictConfig, ListConfig, OmegaConf

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_FILE = REPO_ROOT / "scripts" / "schedule.yaml"
BASE_CONFIG = REPO_ROOT / "configs" / "base.yaml"
COUNTRY_CONFIG_DIR = REPO_ROOT / "configs" / "countries"
WORK_DIR = Path(os.environ.get("OEX_DATA_DIR", REPO_ROOT)) / ".sweep"
MANUAL_FREQUENCY = "as needed"
COMMAND_SOURCES = ("osm", "overture")


class ScheduleError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    id: str
    group: str
    command: str
    config: Path
    iso3: str | None
    settings: dict[str, object] = field(default_factory=dict)
    frequency: str = MANUAL_FREQUENCY
    workers: int = 1
    closes: str | None = None

    def overrides(self) -> dict[str, object]:
        return {**({"iso3": self.iso3} if self.iso3 else {}), **self.settings}


def expiry_date(value: object) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ScheduleError(f"expires {value!r} is not a YYYY-MM-DD date") from error


def frequency_days(value: object) -> int | None:
    """HDX's day count for a frequency name, negative for never and as needed; None if unknown."""
    days = Dataset.transform_update_frequency(str(value))
    return int(days) if days is not None and str(days).lstrip("-").isdigit() else None


def commands_for(config: Path) -> list[str]:
    """The sources a config enables, each a job of its own."""
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    source = raw.get("source")
    if source is None:
        raise ScheduleError(f"{config}: no `source:` block, cannot tell osm from overture apart")
    commands = [name for name in COMMAND_SOURCES if (source.get(name) or {}).get("enabled", True)]
    if not commands:
        raise ScheduleError(f"{config}: neither source.osm nor source.overture is enabled")
    return commands


def write_merged(name: str, config: DictConfig | ListConfig) -> Path:
    """Writes a merged config under WORK_DIR/merged with interpolations, so no secret lands."""
    target = WORK_DIR / "merged" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    # Replaced whole, so a job loading it while another sweep resolves never reads half a file.
    handle, staged = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(OmegaConf.to_yaml(config, resolve=False))
    os.replace(staged, target)
    return target
