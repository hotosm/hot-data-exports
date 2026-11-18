# Add an event

An event export covers one area, such as a flood or an earthquake zone. Each event is a
config in
[`configs/events/`](https://github.com/hotosm/osm-country-exports/tree/main/configs/events).
The sweep merges it over
[`configs/_event-base.yaml`](https://github.com/hotosm/osm-country-exports/blob/main/configs/_event-base.yaml),
which holds what every event shares: the HDX account, the S3 bucket, file formats, and
OpenStreetMap read live through Postpass with the Raw Data API as fallback. An event file
holds only its own settings, and anything it sets wins.

## 1. Copy the template

Copy
[`configs/events/_event-template.yaml`](https://github.com/hotosm/osm-country-exports/blob/main/configs/events/_event-template.yaml)
to `configs/events/<ISO3>-<hazard>-<year>-<name>.yaml`.

## 2. Fill in the copy

```yaml
iso3: NPL
key: hot_flood
frequency: daily

boundary:
  geom: '{"type":"Polygon","coordinates":[...]}'
  buffer_meters: 200

output:
  s3:
    folder: NPL_FLOOD

hdx:
  time_period_start: "2026-08-26"
  combine: true
  combined:
    name: hot_flood_npl
    title: Nepal Flood 2026 Flood Affected Area, Bhote Koshi and Trishuli
    source: OpenStreetMap contributors
    tags: [flooding, affected area, geodata]
    notes: |
      What the dataset covers and how to read it.
    caveats: |
      What it leaves out.

categories_file: configs/_hot-schema.yaml
```

- `key` starts the HDX dataset names; give each event its own.
- `frequency` is how often the export runs, and what HDX shows.
- `boundary.geom` is the area as GeoJSON; `buffer_meters` widens it.
- `output.s3.folder` keeps the files apart from the country export.
- `hdx.time_period_start` starts the dataset's time period when the layers don't carry a
  date column. It must be on or before every source's snapshot; with Overture enabled,
  that is the Overture release the run reads.
- `hdx.combined` `name`, `title`, `notes`, `caveats`, `source` and `tags` are what HDX
  shows; rewrite each one for the event.
- `categories_file` lists the layers: `configs/_hot-schema.yaml` for the standard HOT
  layers, or a file such as `configs/events/_npl-flood-categories.yaml`. An event can
  also list its own layers under `categories:`, as
  [`DEMO-FL-2026-pokhara.yaml`](https://github.com/hotosm/osm-country-exports/blob/main/configs/events/DEMO-FL-2026-pokhara.yaml)
  does. Files starting with `_` are shared and never run alone.
- Overture runs too unless the file sets `source.overture.enabled: false`.

## 3. Run it

Run these on the export server, from the repository folder, with a `.env` that holds the
HDX and S3 credentials:

```bash
set -a && source .env && set +a
./scripts/sweep.py --group events --frequency daily --dry-run
uv run oex-cli all --config .sweep/merged/events/NPL-FL-2026-flood.yaml --no-hdx-push
uv run oex-cli all --config .sweep/merged/events/NPL-FL-2026-flood.yaml --hdx-push
```

The dry run writes each event, merged over the base, to `.sweep/merged/events/` and prints
its commands; pass the event's own frequency. The first export writes the output to
`output/` for checking and publishes nothing to HDX. The second publishes. `all` runs every
enabled source; `osm` runs OpenStreetMap alone.

The config decides which sources the dataset holds. Setting `source.overture.enabled` or
`source.osm.enabled` to `false` removes that source's files from the dataset at the next
publish.

## 4. Keep it updating

Set `frequency:` to `daily`, `weekly` or `monthly`. The export server checks `main` every
5 minutes. After a merge that adds an event file with one of these frequencies, it
publishes the file once the server lock is free, then keeps it on that frequency. The
`events` group's `overrides:` in `scripts/schedule.yaml` can change when a file runs,
disable it, or set an `expires:` date, without changing what HDX shows.

When the event ends, set `frequency:` back to `as needed` and run it once so HDX shows
"As needed".
