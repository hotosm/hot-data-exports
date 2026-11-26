# Add an event

An event export covers one area, such as a flooded area or an earthquake zone. Each event is
one file in
[`configs/events/`](https://github.com/hotosm/hot-data-exports/tree/main/configs/events).
Settings every event shares, such as the HDX account and file formats, are in
[`configs/_event-base.yaml`](https://github.com/hotosm/hot-data-exports/blob/main/configs/_event-base.yaml);
a value set in the event file wins.

## 1. Copy the template

Copy
[`configs/events/_event-template.yaml`](https://github.com/hotosm/hot-data-exports/blob/main/configs/events/_event-template.yaml)
to `configs/events/<ISO3>-<hazard>-<year>-<name>.yaml`:

- `<ISO3>` is the country's three-letter ISO code in capitals, such as `NPL` for Nepal.
- `<hazard>` is the two letters a [GLIDE number](https://glidenumber.net/glide/public/about.jsp)
  starts with, such as `FL` for flood or `EQ` for earthquake.

Use only letters, digits and `-`, and don't start the name with `_`; those files are shared
settings. The file name names the event:

| From `NPL-FL-2026-pokhara.yaml` | Value |
|---|---|
| `iso3`, the country | `NPL` |
| `output.s3.folder`, where the files go on S3 | `NPL_FL_2026_POKHARA` |
| `hdx.combined.name`, the HDX dataset's address | `hotosm_npl_fl_2026_pokhara` |

To use another value, set that key in the file. `output.stac.group` sets the catalog the
event is listed in.

## 2. Fill in the copy

Replace every `CHANGE_ME` and every example value in the copy:

- `frequency` is how often the export runs and what HDX shows, such as `daily`,
  `every 2 days` or `weekly`.
- `expires` is the last day the export runs.
- `boundary.geom` is the area. It can be:
    - the path of a GeoJSON file added to the repository, written from the repository
      root, such as `configs/events/NPL-FL-2026-pokhara.geojson`;
    - a link to a GeoJSON, GeoPackage or KML file with one layer;
    - GeoJSON pasted between the quotes after `geom:`, for example drawn on
      [geojson.io](https://geojson.io).

    Several areas, such as separate towns, export together as one dataset. Add
    `buffer_meters:` under `boundary:` to widen the area.

- `hdx.time_period_start` is the first day the dataset covers, such as the event date. It
  must be on or before the day the export runs, or the publish fails.
- `hdx.combined` `title`, `notes`, `caveats`, `source` and `tags` are what HDX shows.
- `categories_file` lists the layers: `configs/_hot-schema.yaml` for the standard HOT
  layers, or a file such as `configs/events/_npl-flood-categories.yaml`. An event can
  also list its own layers under `categories:`, as
  [`VEN-EQ-2026-000093.yaml`](https://github.com/hotosm/hot-data-exports/blob/main/configs/events/VEN-EQ-2026-000093.yaml)
  does.

Overture runs too unless the file sets `source.overture.enabled: false`. Turning a source
off removes its files from the dataset at the next publish. A file that still holds
`CHANGE_ME` is refused.

## 3. Open a pull request

Add the file to the repository in a pull request; GitHub can
[create the file in the browser](https://docs.github.com/en/repositories/working-with-files/managing-files/creating-new-files).
The pull request runs a check that names any file the export server would refuse. The check
doesn't open the area file or link. Merge once it passes, and the export server publishes it
(step 4).

A maintainer can check an event before merging, in a clone of the repository with a `.env`
that holds the HDX and S3 credentials:

```bash
set -a && source .env && set +a
./scripts/sweep.py --group events --frequency "<frequency>" --dry-run
uv run oex-cli all --config .sweep/merged/events/<event>.yaml \
  --no-hdx-push --no-s3
```

The export writes the files to `output/` (or `$OEX_DATA_DIR/output/`) and publishes
nothing.

## 4. Keep it updating

A merge builds a new image. Once the image is deployed, the event runs within 5 minutes, after
any export already running on the same machine or worker group. It then runs on its
`frequency` until `expires`. A maintainer can change when an event runs in
[the schedule](../reference/schedule.md).

The day after `expires`, the scheduler sets the dataset's update frequency on HDX to
"As needed", once. To end an event early, set `expires` to yesterday and merge; the
next events run closes it. An event that sets `hdx.combine: false` isn't closed
this way; set its HDX update frequency by hand.
