# Deploy

The exports need uv, just, git, `flock` from util-linux, osmium-tool for planet extracts, a
disk for `OEX_DATA_DIR`, and AWS credentials that can write to the bucket.

On one Linux machine:

1. Clone the repository on that disk and follow [Set up](operate.md#set-up).
2. Copy `infra/server/*.service` and `infra/server/*.timer` to `/etc/systemd/system/`, and
   set `User` and the checkout path in each service.
3. Start the timers:

    ```bash
    sudo systemctl enable --now oex-daily.timer oex-watch.timer
    ```

`oex-daily` pulls `main` and runs every due export at 02:00 UTC. Every 5 minutes, `oex-watch`
pulls `main` and, when `configs/events/` changed, runs the due events. Leave the checkout on
`main` without local changes, or the pull fails.

Under another scheduler, run `./scripts/sweep.py --due` once a day with the same
environment, and `./scripts/sweep.py --group events --due` more often to publish merged
events sooner. Keep `OEX_DATA_DIR`, or the checkout when it is unset, between runs: `.sweep/`
records which expired events were closed. Run one scheduler per bucket: the locks only stop
overlapping runs on one machine. GitHub-hosted runners suit events only; a country export
outgrows their [6-hour job limit](https://docs.github.com/en/actions/reference/limits) and
[14 GB disk](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
