# syntax=docker/dockerfile:1
# wake-proxy. Build from the repo root:
#   docker build -f proxy/Dockerfile -t wake-proxy .
FROM rust:1.90-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY proxy/ .
RUN cargo build --release --locked --bin wake-proxy
RUN mkdir -p /out/etc/wake/identity

FROM gcr.io/distroless/cc-debian12:nonroot
COPY --from=build /src/target/release/wake-proxy /wake-proxy
COPY --from=build --chown=65532:65532 /out/etc/wake /etc/wake
USER nonroot
# Reachable only by agentgateway: anything that can call it can get events signed.
EXPOSE 9001
ENTRYPOINT ["/wake-proxy"]
CMD ["serve", "--listen", "0.0.0.0:9001", "--key", "/etc/wake/identity/proxy.key", "--cert", "/etc/wake/identity/proxy.pem"]
