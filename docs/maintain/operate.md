# Run and operate

## Set up

```bash
just setup
cp .env.example .env
```

Fill in the required values in `.env`. Point `OEX_DATA_DIR` at a disk with room for the
[planet file](https://planet.openstreetmap.org/pbf/).

## Run exports

```bash
set -a && source .env && set +a
just sweep --due
just sweep --due --dry-run
just sweep --group big --only USA
just sweep --group normal --workers 4
just check
```

`--due` runs whatever is due, as described in
[Schedule groups and frequencies](../reference/schedule.md). `just check` loads every config
the schedule names, as oex would, and runs nothing; pull requests run it too. Run
`just sweep --help` for every option. A sweep skips layers already uploaded from the same OSM
data; a country sweep also skips layers unchanged since their last publish. `--no-hdx-push`
skips HDX, but files still upload to S3.

## Tasking Manager projects

```bash
just tm
just tm-sandbox
```

Each writes a config from `configs/_tm-template.yaml` for every project active in the last
24 hours and deletes every other config in the group's folder. It then runs the folder.
Run `just tm-configs --help` for the options.

## Change the layers

Edit
[`configs/_hot-schema.yaml`](https://github.com/hotosm/hot-data-exports/blob/main/configs/_hot-schema.yaml).
A change applies to every country on its next run. oex ships a similar
[example](https://github.com/osgeonepal/oex/blob/main/configs/examples/hot-schema.yaml);
reconcile the two by hand when it changes.

## Check the results

- The [update status page](https://hotosm.github.io/hot2hdx-status/) shows overdue datasets.
- `.sweep/merged/<ISO3>.yaml` holds the config the last sweep resolved for a country, before
  command-line options. `.sweep/` is in `OEX_DATA_DIR` when it is set, otherwise in the
  repository.

To run the exports on a schedule, see [Deploy](deploy.md).
