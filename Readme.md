# hot-data-exports

HOT's OpenStreetMap exports for every country and disaster event, published to HDX, and for
Tasking Manager projects, published to S3, run with [`oex`](https://github.com/osgeonepal/oex).

The [documentation](https://hotosm.github.io/hot-data-exports/) covers using, maintaining
and deploying the exports; [Run and operate](https://hotosm.github.io/hot-data-exports/maintain/operate/)
covers setup. Its source is in `docs/`; preview it with `just docs`. `just --list` shows every
recipe.

## Develop against a local oex

Install it into the environment without editing `pyproject.toml`, so a deployment keeps the
pinned release:

```bash
uv pip install -e ../path/to/oex
export UV_NO_SYNC=1
```

`uv run` otherwise re-syncs from the lock file and replaces the editable install.

## Source geometry

`just source-geometry CONFIG NAME` publishes to HDX the geometry a buffered event boundary
was widened from. The uploaded filename becomes the resource's public URL, so pass
`--file-name` to keep an existing resource at the address people already have.
