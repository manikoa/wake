# syntax=docker/dockerfile:1
# The Wake collector, with wake-keygen for creating its keys. Build from the repo root:
#   docker build -f collector/Dockerfile -t wake-collector .
FROM golang:1.27.3-bookworm AS build
RUN go install go.opentelemetry.io/collector/cmd/builder@v0.164.0
WORKDIR /src
COPY log/ log/
COPY collector/ collector/
WORKDIR /src/collector
RUN CGO_ENABLED=0 builder --config builder-config.yaml
RUN CGO_ENABLED=0 go build -C /src/log -o /out/wake-keygen ./cmd/wake-keygen
# Directories volumes mount over, owned by the user the collector runs as, so new
# volumes start out writable by it.
RUN mkdir -p /out/var/lib/wake /out/etc/wake/keys /out/etc/wake/identity

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /src/collector/build/wake-collector /wake-collector
COPY --from=build /out/wake-keygen /wake-keygen
COPY --from=build --chown=65532:65532 /out/var/lib/wake /var/lib/wake
COPY --from=build --chown=65532:65532 /out/etc/wake /etc/wake
COPY collector/config.docker.yaml /etc/wake/collector.yaml
USER nonroot
EXPOSE 4318
ENTRYPOINT ["/wake-collector"]
CMD ["--config", "/etc/wake/collector.yaml"]
