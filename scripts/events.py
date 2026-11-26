import posixpath
import re
from pathlib import Path
from typing import cast

from botocore.exceptions import BotoCoreError, ClientError
from oex.catalog import S3CatalogStore, layout
from oex.config.loader import load_config
from oex.config.schema import RootConfig
from oex.sources.base import exports_category
from oex.stac import published_item_key, writes_items
from omegaconf import DictConfig, OmegaConf
from omegaconf.errors import OmegaConfBaseException

import jobs
from jobs import MANUAL_FREQUENCY, Job, ScheduleError, commands_for, expiry_date

PLACEHOLDER = "CHANGE_ME"
CLOSE_COMMAND = "metadata"
CLOSED_FILE = "closed.json"
EVENT_STEM = re.compile(r"[A-Za-z0-9-]+")
# CKAN package names are 2 to 100 characters: https://docs.ckan.org/en/latest/api/index.html
HDX_NAME_LIMIT = 100


def event_names(stem: str, combine: bool) -> dict:
    """Defaults an event's filename gives: S3 folder, HDX dataset name, and the leading ISO3."""
    if not EVENT_STEM.fullmatch(stem):
        raise ScheduleError(f"{stem}.yaml: use only letters, digits and - in an event filename")
    names: dict = {"output": {"s3": {"folder": stem.upper().replace("-", "_")}}}
    if combine:
        dataset = "hotosm_" + stem.lower().replace("-", "_")
        if len(dataset) > HDX_NAME_LIMIT:
            raise ScheduleError(
                f"{stem}.yaml: the HDX name {dataset} is over {HDX_NAME_LIMIT} characters"
            )
        names["hdx"] = {"combined": {"name": dataset}}
    country = stem.split("-", 1)[0]
    if len(country) == 3 and country.isalpha():
        if not country.isupper():
            raise ScheduleError(
                f"{stem}.yaml: start with the ISO3 code in capitals, such as NPL-FL-2026-name"
            )
        names["iso3"] = country
    return names


def merge_event(config: Path, base: Path) -> DictConfig:
    """An event file over the group base and its filename defaults; what the file sets wins."""
    if PLACEHOLDER in config.read_text(encoding="utf-8"):
        raise ScheduleError(f"{config.name} still holds {PLACEHOLDER}; replace every one")
    base_config, event = OmegaConf.load(base), OmegaConf.load(config)
    combine = bool(OmegaConf.select(OmegaConf.merge(base_config, event), "hdx.combine"))
    merged = cast(
        DictConfig,
        OmegaConf.merge(base_config, OmegaConf.create(event_names(config.stem, combine)), event),
    )
    # The schedule reads `expires`; oex has no such setting and refuses unknown keys.
    merged.pop("expires", None)
    return merged


def closed_key(cfg: RootConfig) -> str | None:
    """Where the event's close is recorded: beside its STAC catalog, so every host sees it."""
    place = layout(cfg)
    return None if place is None else posixpath.join(posixpath.dirname(place.area.key), CLOSED_FILE)


def ever_published(cfg: RootConfig, config: Path, store: S3CatalogStore) -> bool:
    """Whether any of the event's layers reached HDX, read from its dated STAC Items."""
    if not writes_items(cfg):
        # Undated Items cannot tell; the close runs and fails loudly if HDX lacks the dataset.
        return True
    return any(
        (store.read(published_item_key(cfg, category, source))[0] or {})
        .get("properties", {})
        .get("updated")
        for source in commands_for(config)
        for category in cfg.categories
        if exports_category(cfg, category, source)
    )


def closing_job(group: str, base: Path, config: Path, expires: object) -> Job | str:
    """The one HDX update telling an expired event's users it stopped, or why none runs."""
    expiry = str(expiry_date(expires))
    event = merge_event(config, base)
    if not OmegaConf.select(event, "hdx.push") or event.get("frequency") == MANUAL_FREQUENCY:
        return "nothing to close on HDX"
    if not OmegaConf.select(event, "hdx.combine"):
        return "not combined, so set its HDX frequency by hand"
    event.frequency = MANUAL_FREQUENCY
    closing = jobs.write_merged(f"{group}/{config.name}", event)
    try:
        cfg = load_config(closing)
        key = closed_key(cfg)
        if key is None:
            return "no STAC catalog on S3 to record the close in, so set its HDX frequency by hand"
        store = S3CatalogStore(cfg)
        if (store.read(key)[0] or {}).get("expires") == expiry:
            return "closed on HDX"
        if not ever_published(cfg, closing, store):
            return "never published, nothing to close on HDX"
    except (ValueError, OmegaConfBaseException, BotoCoreError, ClientError) as error:
        raise ScheduleError(f"{config.name}: cannot read its published layers: {error}") from error
    return Job(f"{group}/{config.stem}:close", group, CLOSE_COMMAND, closing, None, closes=expiry)
