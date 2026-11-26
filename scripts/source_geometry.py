#!/usr/bin/env -S uv run python
"""Publish the geometry a buffered extraction boundary was derived from, to HDX."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from hdx.api.configuration import Configuration
from hdx.data.dataset import Dataset
from hdx.data.resource import Resource
from oex.boundary import area_sq_km, read_geometry
from oex.config.loader import load_config
from oex.config.schema import RootConfig
from oex.naming import COMBINED_SLUG, combined_dataset_name
from oex.s3 import artifact_key, resolve_with_bucket, upload

EPILOG = """examples:
  source_geometry.py --config configs/events/NPL-FL-2026-flood-extent.yaml \\
      --name "Flood Extent, Observed 27 August 2026, GeoJSON"

oex publishes the buffered boundary as the Area of Interest; this publishes what it was
widened from, so the difference is visible.

exit codes: 1 the upload or HDX call failed, 2 the config has no readable buffered boundary."""
DESCRIPTION = "The geometry the area of interest was built from, before the buffer was applied."


def publish(
    cfg: RootConfig,
    s3: tuple[str, str, str, str | None, str],
    geometry: dict,
    area: float,
    name: str,
    category: str,
    file_name: str,
) -> str:
    """The dataset is read before the upload, so a wrong name uploads nothing."""
    bucket, prefix, region, endpoint_url, acl = s3
    hdx_key = os.environ.get("HDX_API_KEY") or sys.exit("error: HDX_API_KEY is not set")
    Configuration.create(hdx_site=cfg.hdx.site, hdx_key=hdx_key, user_agent=cfg.hdx.user_agent)
    dataset_name = combined_dataset_name(cfg)
    dataset = Dataset.read_from_hdx(dataset_name) or sys.exit(
        f"error: HDX dataset {dataset_name} not found"
    )
    feature = {"type": "Feature", "properties": {"name": name, "area_sq_km": round(area, 2)}}
    collection = {"type": "FeatureCollection", "features": [{**feature, "geometry": geometry}]}
    with tempfile.TemporaryDirectory() as work_dir:
        path = Path(work_dir) / file_name
        path.write_text(json.dumps(collection, ensure_ascii=False, indent=1), encoding="utf-8")
        key = artifact_key(
            prefix,
            cfg.iso3,
            category,
            file_name,
            folder=cfg.output.s3.folder,
            nest_by_category=cfg.output.s3.nest_by_category,
        )
        url = upload(
            path, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl
        )
        resource = Resource(
            {
                "name": name,
                "description": DESCRIPTION,
                "format": "geojson",
                "size": path.stat().st_size,
                "url": url,
            }
        )
    resource.mark_data_updated()
    dataset.add_update_resource(resource)
    dataset.update_in_hdx(
        match_resources_by_metadata=True, hxl_update=False, remove_additional_resources=False
    )
    return url


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--name", required=True, help="HDX resource name for the geometry")
    parser.add_argument("--category", default=COMBINED_SLUG, help="S3 path segment")
    parser.add_argument(
        "--file-name", help="uploaded filename, default <dataset>_<category>.geojson"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg.boundary.geom or not cfg.boundary.buffer_meters:
        print(
            "error: the config needs boundary.geom and boundary.buffer_meters; without a buffer, "
            "oex already publishes this geometry as the Area of Interest",
            file=sys.stderr,
        )
        return 2
    try:
        geometry = read_geometry(cfg.boundary.geom)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    file_name = args.file_name or f"{combined_dataset_name(cfg)}_{args.category}.geojson"
    area = area_sq_km(geometry)
    print(f"  {args.name}\n    {file_name}  {area:.2f} sq km")
    if args.dry_run:
        return 0
    try:
        s3 = resolve_with_bucket(cfg.output.s3)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    url = publish(cfg, s3, geometry, area, args.name, args.category, file_name)
    print(f"    -> {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
