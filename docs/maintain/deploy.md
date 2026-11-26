# Deploy

A push to `main` that changes the configs or the code publishes the image
`ghcr.io/hotosm/hot-data-exports`, tagged `main` and `windmill-main`. It runs on a schedule:

| Command | When |
|---|---|
| `sweep.py --due --wait` | daily |
| `sweep.py --group events --due` | every 5 minutes |

Each run needs the variables in
[`.env.example`](https://github.com/hotosm/hot-data-exports/blob/main/.env.example) and a disk at
`/data` with room for the [planet file](https://planet.openstreetmap.org/pbf/). A run waits for
another using `/data` with `--wait`, or exits 3 without it. Give each S3 bucket one scheduler.

## One machine

1. Install podman, and write the variables to `/etc/hot-data-exports.env` as `NAME=value`
   lines, readable only by root.
2. Copy `infra/server/*` to `/etc/systemd/system/`, set the data folder and memory cap in both
   services, and start the timers:

    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable --now oex-daily.timer oex-watch.timer
    ```

Read a job's log with `journalctl -u oex-daily -u oex-watch -g 'normal/ZWE'`.

## Windmill

1. Run each worker group on the `windmill-main` tag with one worker, pull policy `Always` and
   one `ReadWriteMany` volume at `/data`, shared by all groups.
2. Set the variables on the workers, and list their names and `OEX_DATA_DIR` in
   [`WHITELIST_ENVS`](https://www.windmill.dev/docs/core_concepts/environment_variables).
   Windmill passes a job only the variables listed there.
3. Schedule one Bash script per group: `events` every 5 minutes, and `priority`, `normal`,
   `high_income` and `big` daily.

    ```bash
    /app/.venv/bin/python /app/scripts/sweep.py --group normal --due --wait
    ```

Restart the workers after each new image. Each run's page holds its log.
