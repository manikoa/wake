# syntax=docker/dockerfile:1
# The reference fleet's TypeScript agent, notify, on Wake's TypeScript SDK. Build from the
# repo root:
#   docker build -f examples/fleet/notify/Dockerfile -t wake-fleet-notify .
FROM node:24-bookworm-slim AS build
RUN npm install --global pnpm@12.15.0
WORKDIR /src
# The SDK first, built from source, then the agent, which takes it as a file: dependency.
# Building the SDK needs its development dependencies but none of their optional ones, such
# as the Claude Agent SDK's native Claude Code, nearly 300 MB nothing here runs. Every
# image the game day builds has to fit one CI runner's disk.
COPY sdk/typescript/package.json sdk/typescript/pnpm-lock.yaml sdk/typescript/tsconfig.json sdk/typescript/tsconfig.build.json sdk/typescript/
RUN cd sdk/typescript && pnpm install --frozen-lockfile --no-optional
COPY sdk/typescript/src sdk/typescript/src
RUN cd sdk/typescript && pnpm build
COPY examples/fleet/notify/package.json examples/fleet/notify/pnpm-lock.yaml examples/fleet/notify/tsconfig.json examples/fleet/notify/
RUN cd examples/fleet/notify && pnpm install --frozen-lockfile
COPY examples/fleet/notify/src examples/fleet/notify/src
RUN cd examples/fleet/notify && pnpm typecheck && pnpm prune --prod

FROM node:24-bookworm-slim
# python3 only for the fleet's reach check (reach.py), which run.sh runs in every agent.
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/examples/fleet/notify /src/examples/fleet/notify
COPY examples/fleet/reach.py /src/examples/fleet/reach.py
WORKDIR /src/examples/fleet/notify
USER node
CMD ["node", "src/notify.ts"]
