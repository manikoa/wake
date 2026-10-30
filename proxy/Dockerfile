# syntax=docker/dockerfile:1
# wake-proxy. Build from the repo root:
#   docker build -f proxy/Dockerfile -t wake-proxy .
FROM rust:1.98-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e AS build
# libprotobuf-dev has the well-known types (google/protobuf/*.proto) the protos import.
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY proxy/ .
RUN cargo build --release --locked --bin wake-proxy
# Directories volumes mount over, owned by the user the proxy runs as.
RUN mkdir -p /out/etc/wake/identity /out/etc/wake/gateway /out/var/lib/wake-proxy /out/run/wake

FROM gcr.io/distroless/cc-debian12:nonroot@sha256:9dac0a79194e45a7da0158a9c6da57b217585af0786db3845d1f0ec1a0dd182f
COPY --from=build /src/target/release/wake-proxy /wake-proxy
COPY --from=build --chown=65532:65532 /out/etc/wake /etc/wake
COPY --from=build --chown=65532:65532 /out/var/lib/wake-proxy /var/lib/wake-proxy
COPY --from=build --chown=65532:65532 /out/run/wake /run/wake
USER nonroot
# The hooks are a Unix socket in /run/wake, for a volume only agentgateway shares: anything
# that can call them can get events signed. 9002 is the brake's control API, mTLS with the
# brake's SVID. 9003 serves memory adapters their quarantine rules, read only, and 9464
# Prometheus its metrics, with --metrics-listen.
EXPOSE 9002 9003 9464
ENTRYPOINT ["/wake-proxy"]
# The halts state must be initialized once (wake-proxy init --state-dir /var/lib/wake-proxy);
# until it is, every call is denied.
CMD ["serve", "--listen", "unix:/run/wake/proxy.sock", "--key", "/etc/wake/identity/proxy.key", "--cert", "/etc/wake/identity/proxy.pem", \
     "--state-dir", "/var/lib/wake-proxy"]
