# syntax=docker/dockerfile:1
# The reference fleet's agents and tools, in one image. Build from the repo root:
#   docker build -f examples/fleet/Dockerfile -t wake-fleet .
FROM ghcr.io/astral-sh/uv:0.7.13 AS uv

FROM python:3.13-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY sdk/python sdk/python
COPY examples/fleet examples/fleet
WORKDIR /src/examples/fleet
RUN uv sync --locked --no-dev
ENV PATH=/src/examples/fleet/.venv/bin:$PATH
USER nobody
