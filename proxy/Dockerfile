# syntax=docker/dockerfile:1
# wake-proxy. Build from the repo root:
#   docker build -f proxy/Dockerfile -t wake-proxy .
FROM rust:1.90-bookworm AS build
# libprotobuf-dev has the well-known types (google/protobuf/*.proto) the protos import.
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY proxy/ .
RUN cargo build --release --locked --bin wake-proxy
# Directories volumes mount over, owned by the user the proxy runs as.
RUN mkdir -p /out/etc/wake/identity /out/var/lib/wake-proxy

FROM gcr.io/distroless/cc-debian12:nonroot
COPY --from=build /src/target/release/wake-proxy /wake-proxy
COPY --from=build --chown=65532:65532 /out/etc/wake /etc/wake
COPY --from=build --chown=65532:65532 /out/var/lib/wake-proxy /var/lib/wake-proxy
USER nonroot
# 9001 is reachable only by agentgateway: anything that can call it can get events signed.
# 9002 is the brake's control API, mTLS with the brake's SVID. 9003 serves memory adapters
# their quarantine rules, read only.
EXPOSE 9001 9002 9003
ENTRYPOINT ["/wake-proxy"]
CMD ["serve", "--listen", "0.0.0.0:9001", "--key", "/etc/wake/identity/proxy.key", "--cert", "/etc/wake/identity/proxy.pem", \
     "--state-dir", "/var/lib/wake-proxy"]
