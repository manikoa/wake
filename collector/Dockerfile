# syntax=docker/dockerfile:1
# The Wake collector, with wake-keygen for creating its keys and wake-log for checking the
# log it keeps. Build from the repo root:
#   docker build -f collector/Dockerfile -t wake-collector .
FROM golang:1.27.3-bookworm@sha256:972b4110ba0f47539a975c31e52c64765e1ff19f2d74469ae4a595c6676e80a5 AS build
RUN go install go.opentelemetry.io/collector/cmd/builder@v0.164.0
WORKDIR /src
COPY log/ log/
COPY collector/ collector/
WORKDIR /src/collector
RUN CGO_ENABLED=0 builder --config builder-config.yaml
RUN CGO_ENABLED=0 go build -C /src/log -o /out/wake-keygen ./cmd/wake-keygen
RUN CGO_ENABLED=0 go build -C /src/log -o /out/wake-log ./cmd/wake-log
# Directories volumes mount over, owned by the user the collector runs as, so new
# volumes start out writable by it.
RUN mkdir -p /out/var/lib/wake /out/etc/wake/keys /out/etc/wake/identity

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /src/collector/build/wake-collector /wake-collector
COPY --from=build /out/wake-keygen /wake-keygen
COPY --from=build /out/wake-log /wake-log
COPY --from=build --chown=65532:65532 /out/var/lib/wake /var/lib/wake
COPY --from=build --chown=65532:65532 /out/etc/wake /etc/wake
COPY collector/config.docker.yaml /etc/wake/collector.yaml
USER nonroot
# OTLP, the health check, and the collector's own metrics for Prometheus.
EXPOSE 4318 13133 8888
ENTRYPOINT ["/wake-collector"]
CMD ["--config", "/etc/wake/collector.yaml"]
