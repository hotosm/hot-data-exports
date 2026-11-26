#!/usr/bin/env -S uv run python
"""Generate one oex config per active Tasking Manager project.

A project doesn't have a country code, so output.s3.folder carries its id. Production configs
query Postpass for the project polygon; sandbox configs read a PBF cut to it. Without
--project, each run deletes the configs of projects no longer active.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from itertools import batched
from pathlib import Path
from typing import cast

from oex.config.loader import load_config
from oex.config.schema import OsmSourceConfig
from oex.naming import slugify
from oex.osm.runner import refresh_planet_pbf, refreshes_planet
from omegaconf import OmegaConf
from upath import UPath

TM_API = os.environ.get("TM_API") or "https://tasking-manager-production-api.hotosm.org/api/v2"
TM_TIMEOUT_SECONDS = 60
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "configs" / "_tm-template.yaml"
MAX_INTERVAL_HOURS = 24
SANDBOX_PBF_ENV = "TM_SANDBOX_PBF"
EXTRACT_BATCH_ENV = "TM_EXTRACT_BATCH"
# complete_ways needs about 3 GB per extract on the planet: https://docs.osmcode.org/osmium/latest/osmium-extract.html
DEFAULT_EXTRACT_BATCH = 6
# TM mapping_types are 1-based indexes into this order.
MAPPING_TYPES = ("Roads", "Buildings", "Waterways", "Landuse")
DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}")
OSM_KEY = re.compile(r"[A-Za-z0-9_:.-]+")


class TaskingManagerError(Exception):
    pass


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TM_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise TaskingManagerError(f"{url}: {error}") from error


def _display(path: Path) -> str:
    return os.path.relpath(path, REPO_ROOT)


def fetch_active_projects(interval: int, sandbox: bool) -> list[dict]:
    url = f"{TM_API}/projects/queries/active/?interval={interval}"
    if sandbox:
        url += "&sandbox=true"
    payload = fetch_json(url)
    if "features" not in payload:
        raise TaskingManagerError(f"{url}: no `features` in the response: {payload}")
    return payload["features"]


def fetch_project(project_id: str) -> dict:
    """One project as a GeoJSON feature, whatever its recent activity."""
    url = f"{TM_API}/projects/{project_id}/"
    payload = fetch_json(url)
    if "areaOfInterest" not in payload:
        raise TaskingManagerError(f"{url}: no `areaOfInterest` in the response")
    return {
        "geometry": payload["areaOfInterest"],
        "properties": {
            "project_id": payload["projectId"],
            "mapping_types": payload.get("mappingTypes"),
            "custom_mapping_types": payload.get("customMappingTypes"),
        },
    }


def category_names(mapping_types: list | None) -> list[str]:
    """TM mapping types, as 1-based indexes or names like LAND_USE, to template categories."""
    by_name = {name.upper(): name for name in MAPPING_TYPES}
    names = []
    for value in mapping_types or []:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 1 <= value <= len(MAPPING_TYPES):
            names.append(MAPPING_TYPES[value - 1])
        elif isinstance(value, str):
            match = by_name.get(value.upper().replace("_", ""))
            if match:
                names.append(match)
    return names


def custom_categories(custom_types: object) -> list[dict]:
    """TM custom mapping types to categories; a type the TM would reject is skipped, not trusted."""
    categories, taken = [], {slugify(name) for name in MAPPING_TYPES}
    for custom in custom_types if isinstance(custom_types, list) else []:
        custom = custom if isinstance(custom, dict) else {}
        name = str(custom.get("name") or "")
        slug = slugify(name)
        tag_filter = custom_filter(custom.get("tags"))
        attributes = custom.get("attributes") or []
        if (
            not slug
            or slug in taken
            or "$" in name
            or not tag_filter
            or not isinstance(attributes, list)
            or not all(isinstance(key, str) and OSM_KEY.fullmatch(key) for key in attributes)
        ):
            print(f"skip custom mapping type {name!r}: invalid name, tags or attributes")
            continue
        taken.add(slug)
        select = custom_select([*tag_filter, *attributes])
        categories.append(
            {"name": name, "osm": {"enabled": True, "filter": tag_filter, "select": select}}
        )
    return categories


def custom_filter(tags: object) -> dict:
    """`key=value` tags as an oex filter matching any, `key=*` for any value; empty if invalid."""
    if not isinstance(tags, list):
        return {}
    values: dict[str, list[str]] = {}
    for tag in tags:
        key, _, value = str(tag).partition("=")
        if not OSM_KEY.fullmatch(key) or not value or "$" in value:
            return {}
        values.setdefault(key, []).append(value)
    return {key: True if "*" in found else found for key, found in values.items()}


def custom_select(keys: list[str]) -> list[str]:
    """One column per tag key between `id`, `name` and `source`, named as oex names folders."""
    columns = {"id": "feature_id"}
    for key in ("name", *keys, "source"):
        alias = slugify(key)
        # oex appends `geometry AS geom`, and DuckDB needs a leading digit quoted
        if alias and alias != "geom":
            alias = f'"{alias}"' if alias[0].isdigit() else alias
            columns.setdefault(alias, f"tags['{key}']")
    return [f"{expr} AS {alias}" for alias, expr in columns.items()]


def build_config(
    template,
    feature: dict,
    pbf_path: Path | None = None,
    all_categories: bool = False,
):
    """The template filled in for one project, or None when nothing maps."""
    project_id = feature["properties"]["project_id"]
    cfg = OmegaConf.create(OmegaConf.to_yaml(template, resolve=False))
    if not all_categories:
        wanted = category_names(feature["properties"].get("mapping_types"))
        custom = custom_categories(feature["properties"].get("custom_mapping_types"))
        cfg.categories = [c for c in cfg.categories if c.name in wanted] + custom
    if not cfg.categories:
        return None
    cfg.dataset_name = f"Tasking Manager Project {project_id}"
    cfg.boundary.geom = json.dumps(feature["geometry"])
    cfg.output.s3.folder = f"hotosm_project_{project_id}"
    if pbf_path is not None:
        cfg.source.osm.engine = "planet"
        cfg.source.osm.fallback_engine = ""
        cfg.source.osm.pbf_path = str(pbf_path)
        # oex would otherwise date this file against the published planet and download over it.
        cfg.source.osm.auto_download_planet = False
    return cfg


def write_osmium_config(features: Iterable[dict], pbf_dir: Path) -> tuple[Path, dict[str, Path]]:
    """One osmium extract config for these projects, and each project id's output PBF."""
    pbf_dir.mkdir(parents=True, exist_ok=True)
    extracts, outputs = [], {}
    for feature in features:
        project_id = str(feature["properties"]["project_id"])
        polygon = pbf_dir / f"{project_id}.geojson"
        polygon_feature = {"type": "Feature", "properties": {}, "geometry": feature["geometry"]}
        polygon.write_text(json.dumps(polygon_feature), encoding="utf-8")
        output = pbf_dir / f"{project_id}.osm.pbf"
        extracts.append(
            {
                "output": output.name,
                "polygon": {"file_name": str(polygon), "file_type": "geojson"},
            }
        )
        outputs[project_id] = output
    config = pbf_dir / "_osmium-extracts.json"
    config.write_text(
        json.dumps({"directory": str(pbf_dir), "extracts": extracts}, indent=2),
        encoding="utf-8",
    )
    return config, outputs


def run_osmium_extract(source_pbf: Path, config: Path) -> None:
    print(f"osmium extract: one pass over {source_pbf} for {config}")
    subprocess.run(
        ["osmium", "extract", "--config", str(config), "--strategy", "complete_ways"]
        + ["--overwrite", str(source_pbf)],
        check=True,
    )


def resolve_source_pbf(location: str) -> str:
    """The newest PBF when the location ends in `/`, as the sandbox exports to dated folders."""
    if not location.endswith("/"):
        return location
    root = UPath(location)
    dated = sorted(child.name for child in root.iterdir() if DATE_PREFIX.fullmatch(child.name))
    if not dated:
        raise TaskingManagerError(f"{location}: no YYYY-MM-DD folders to pick a source from")
    newest = root / dated[-1]
    pbfs = sorted(child for child in newest.iterdir() if child.name.endswith(".pbf"))
    if not pbfs:
        raise TaskingManagerError(f"{newest}: no .pbf inside the newest folder")
    print(f"source: {pbfs[0]} (newest of {len(dated)} dated folders)")
    return str(pbfs[0])


def ensure_local_pbf(source: str, cache_dir: Path) -> Path:
    """osmium reads local files only, so fetch a remote source once and reuse it."""
    if "://" not in source:
        return Path(source)
    remote = UPath(source)
    cache_dir.mkdir(parents=True, exist_ok=True)
    local = cache_dir / f"{remote.parent.name}-{remote.name}"
    size = remote.stat().st_size
    if local.is_file() and local.stat().st_size == size:
        print(f"source: reusing {_display(local)}")
        return local
    print(f"source: downloading {source} ({size / 1e6:.1f} MB) -> {_display(local)}")
    remote.fs.get_file(remote.path, str(local))
    return local


def extract_source(template_path: Path, sandbox: bool) -> str:
    """The sandbox export named in the environment, or the template's planet for production."""
    if sandbox:
        location = os.environ.get(SANDBOX_PBF_ENV)
        if not location:
            raise TaskingManagerError(f"sandbox extracts need {SANDBOX_PBF_ENV} set")
        return location
    osm = cast(OsmSourceConfig, load_config(template_path).source["osm"])
    if not osm.pbf_path:
        raise TaskingManagerError(f"{template_path}: source.osm.pbf_path is not set")
    if osm.engine.lower() == "planet" and refreshes_planet(osm):
        refresh_planet_pbf(osm, Path(osm.pbf_path))
    return osm.pbf_path


def cut_project_extracts(
    features: list[dict], source: str, target: Path
) -> tuple[dict[str, Path], Path]:
    """Cut each project's PBF, one batch per osmium pass; returns usable extracts and the source."""
    batch_size = int(os.environ.get(EXTRACT_BATCH_ENV, DEFAULT_EXTRACT_BATCH))
    source_pbf = ensure_local_pbf(resolve_source_pbf(source), target)
    if not source_pbf.is_file():
        raise TaskingManagerError(f"source PBF {source_pbf} is not a file")
    outputs: dict[str, Path] = {}
    for batch in batched(features, batch_size, strict=False):
        config, batch_outputs = write_osmium_config(batch, target)
        run_osmium_extract(source_pbf, config)
        outputs.update(batch_outputs)

    usable = {}
    for project_id, path in outputs.items():
        if not path.is_file():
            print(f"warn project {project_id}: osmium wrote no extract, using {source_pbf} whole")
            continue
        if node_count(path) == 0:
            print(
                f"warn project {project_id}: extract is empty, so {source_pbf} does not cover it; "
                "the export would publish nothing"
            )
        usable[project_id] = path
    return usable, source_pbf


def node_count(pbf: Path) -> int:
    """Nodes in a PBF. Zero means the source did not cover that project's polygon."""
    command = ["osmium", "fileinfo", "-e", "-g", "data.count.nodes", str(pbf)]
    return int(subprocess.run(command, stdout=subprocess.PIPE, text=True, check=True).stdout)


def sync(out_dir: Path, configs: dict, dry_run: bool, prune: bool = False) -> int:
    """Writes changed configs; with `prune`, removes project configs no longer listed."""
    if prune:
        generated = {path for path in out_dir.glob("*.yaml") if path.stem.isdigit()}
        for stale in sorted(generated - {out_dir / name for name in configs}):
            print(f"remove {_display(stale)}")
            if not dry_run:
                stale.unlink()
    written = 0
    for name, cfg in sorted(configs.items()):
        text = OmegaConf.to_yaml(cfg, resolve=False)
        target = out_dir / name
        if target.exists() and target.read_text(encoding="utf-8") == text:
            continue
        written += 1
        if not dry_run:
            target.write_text(text, encoding="utf-8")

    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--interval",
        type=int,
        choices=range(1, MAX_INTERVAL_HOURS + 1),
        default=MAX_INTERVAL_HOURS,
        metavar="HOURS",
        help=f"hours of activity, 1 to {MAX_INTERVAL_HOURS} (default {MAX_INTERVAL_HOURS})",
    )
    parser.add_argument("--sandbox", action="store_true", help="sandbox projects")
    parser.add_argument("--out", type=Path, help="output dir (default: the group dir)")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument(
        "--extract",
        action="store_true",
        help=f"cut a PBF per project, {EXTRACT_BATCH_ENV} projects per osmium pass",
    )
    parser.add_argument("--template", type=Path, default=TEMPLATE, help="config template")
    parser.add_argument(
        "--project", action="append", metavar="ID", help="these projects, ignoring --interval"
    )
    args = parser.parse_args()

    out_dir = args.out or REPO_ROOT / "configs" / (
        "tasking_manager_sandbox" if args.sandbox else "tasking_manager"
    )
    if not out_dir.is_dir():
        print(f"tm: {out_dir} is not a directory", file=sys.stderr)
        return 2
    if args.sandbox and not args.extract and not args.dry_run:
        print(
            f"tm: --sandbox needs --extract; {SANDBOX_PBF_ENV} names dated exports", file=sys.stderr
        )
        return 2

    template = OmegaConf.create(args.template.read_text(encoding="utf-8"))
    try:
        if args.project:
            features = [fetch_project(project_id) for project_id in args.project]
        else:
            features = fetch_active_projects(args.interval, args.sandbox)

        kept = []
        for feature in features:
            props = feature["properties"]
            if category_names(props.get("mapping_types")) or props.get("custom_mapping_types"):
                kept.append(feature)
            else:
                print(f"skip project {props['project_id']}: no supported mapping type")

        outputs: dict[str, Path] = {}
        source_pbf = None
        if args.extract and kept and not args.dry_run:
            group = "tm_sandbox" if args.sandbox else "tm"
            pbf_dir = Path(os.environ.get("OEX_DATA_DIR", REPO_ROOT)) / "data" / group
            source = extract_source(args.template, args.sandbox)
            outputs, source_pbf = cut_project_extracts(kept, source, pbf_dir)
    except (TaskingManagerError, subprocess.CalledProcessError) as error:
        print(f"tm: {error}", file=sys.stderr)
        return 2

    configs = {}
    for feature in kept:
        project_id = str(feature["properties"]["project_id"])
        cfg = build_config(
            template, feature, outputs.get(project_id, source_pbf), args.template != TEMPLATE
        )
        if cfg is None:
            print(f"skip project {project_id}: no category matched")
            continue
        configs[f"{project_id}.yaml"] = cfg

    written = sync(out_dir, configs, args.dry_run, prune=not args.project)
    scope = "sandbox" if args.sandbox else "production"
    print(
        f"tm: {scope} active={len(features)} configs={len(configs)} "
        f"written={written} -> {_display(out_dir)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
