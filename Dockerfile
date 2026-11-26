# syntax=docker/dockerfile:1.7
ARG BASE=debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0

FROM ghcr.io/astral-sh/uv:0.11.8-trixie-slim AS builder

# A uv-managed Python at a fixed path, so the environment runs on either base image.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0 AS tippecanoe

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates libsqlite3-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# oex's tippecanoe install script from v0.4.36, the oex release uv.lock pins; update both together.
ADD --chmod=755 --checksum=sha256:6b931395e88fb40feb6ece41028bcb271b856dd0d9198aba1219bb43a9959051 \
    https://raw.githubusercontent.com/osgeonepal/oex/a531bef2c1735406aa820c882af0d3b384ed6b5b/scripts/install-tippecanoe.sh \
    /tmp/install-tippecanoe.sh
RUN /tmp/install-tippecanoe.sh


FROM ${BASE} AS exports

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates osmium-tool \
    && rm -rf /var/lib/apt/lists/*
COPY --from=tippecanoe /usr/local/bin/tippecanoe /usr/local/bin/tippecanoe
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY configs /app/configs
COPY scripts /app/scripts

ENV OEX_DATA_DIR=/data


FROM exports

ENTRYPOINT ["/app/.venv/bin/python", "/app/scripts/sweep.py"]
CMD ["--help"]
