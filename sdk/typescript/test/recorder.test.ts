import assert from "node:assert/strict";
import { AsyncLocalStorage } from "node:async_hooks";
import { createServer as createHttpServer, type IncomingHttpHeaders, type Server } from "node:http";
import { createServer as createTcpServer, type Socket } from "node:net";
import { test } from "node:test";

import { tool } from "@langchain/core/tools";
import { context, ROOT_CONTEXT, trace, type Context, type ContextManager } from "@opentelemetry/api";
import { ExportResultCode, type ExportResult } from "@opentelemetry/core";
import { AlwaysOffSampler, type ReadableSpan, type SpanExporter } from "@opentelemetry/sdk-trace";
import { z } from "zod";

import { Recorder } from "../src/index.ts";
import { WakeCallbackHandler } from "../src/langgraph.ts";
import { attr, payload, recorded, tick, wakeEvents } from "./helpers.ts";
import { ANSWER, ask, emailAgentGraph } from "./email-agent.ts";

class FailingExporter implements SpanExporter {
  failing = true;

  export(_spans: ReadableSpan[], done: (result: ExportResult) => void): void {
    done({ code: this.failing ? ExportResultCode.FAILED : ExportResultCode.SUCCESS });
  }

  async shutdown(): Promise<void> {}
}

/** Holds every export until released, so the queue behind it fills. */
class BlockedExporter implements SpanExporter {
  #released = false;
  #waiting: (() => void)[] = [];

  export(_spans: ReadableSpan[], done: (result: ExportResult) => void): void {
    if (this.#released) done({ code: ExportResultCode.SUCCESS });
    else this.#waiting.push(() => done({ code: ExportResultCode.SUCCESS }));
  }

  release(): void {
    this.#released = true;
    for (const done of this.#waiting.splice(0)) done();
  }

  async shutdown(): Promise<void> {
    this.release();
  }
}

function emit(recorder: Recorder, count: number): void {
  for (let i = 0; i < count; i++) recorder.tracer.startSpan(`event ${i}`).end();
}

test("counts sent events", async () => {
  const { recorder } = recorded("email-agent");
  await ask(emailAgentGraph(), new WakeCallbackHandler(recorder));
  await recorder.flush();
  const health = recorder.health();
  assert.ok(health.ok);
  assert.equal(health.sent, 4); // the agent run plus three events
  assert.equal(health.dropped, 0);
});

test("counts failed exports and calls onDrop", async () => {
  const drops: [number, string][] = [];
  const exporter = new FailingExporter();
  const recorder = new Recorder({ agent: "email-agent", exporter, onDrop: (n, why) => drops.push([n, why]) });
  const result = await ask(emailAgentGraph(), new WakeCallbackHandler(recorder));
  await recorder.flush();

  assert.equal(result.messages.at(-1)?.content, ANSWER);
  const health = recorder.health();
  assert.ok(!health.ok);
  assert.equal(health.dropped, 4);
  assert.equal(drops.reduce((sum, [n]) => sum + n, 0), 4);

  // Once the collector is back, the recorder reports healthy again but keeps the count.
  exporter.failing = false;
  emit(recorder, 1);
  await recorder.flush();
  assert.ok(recorder.health().ok);
  assert.equal(recorder.health().dropped, 4);
});

test("counts events pushed out of a full queue, and reports them only after the export", async () => {
  const reported: [number, string][] = [];
  const exporter = new BlockedExporter();
  const recorder = new Recorder({ agent: "email-agent", exporter, maxQueueSize: 8, onDrop: (n, why) => reported.push([n, why]) });
  emit(recorder, 8); // a full batch starts an export, which blocks
  await tick(20);
  // Eight more fill the queue and the next twelve push spans out.
  emit(recorder, 20);
  assert.equal(recorder.health().dropped, 12);
  await tick(20);
  assert.deepEqual(reported, [], "never reported while the agent records");
  exporter.release();
  await recorder.flush();
  assert.equal(recorder.health().sent, 16);
  assert.deepEqual(reported, [[12, "queue full"]]);
});

test("onDrop errors never reach the agent", async () => {
  const recorder = new Recorder({
    agent: "email-agent",
    exporter: new FailingExporter(),
    onDrop: () => {
      throw new Error("pager is down");
    },
  });
  const result = await ask(emailAgentGraph(), new WakeCallbackHandler(recorder));
  await recorder.flush();
  assert.equal(result.messages.at(-1)?.content, ANSWER);
  assert.equal(recorder.health().dropped, 4);
});

/** A stand-in OTLP/HTTP collector that accepts every request, and notes each one's headers. */
async function collector(): Promise<{ endpoint: string; seen: [IncomingHttpHeaders, Buffer][]; kill: () => Promise<void> }> {
  const seen: [IncomingHttpHeaders, Buffer][] = [];
  const sockets = new Set<Socket>();
  const server: Server = createHttpServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (c: Buffer) => chunks.push(c));
    request.on("end", () => {
      seen.push([request.headers, Buffer.concat(chunks)]);
      response.writeHead(200, { "Content-Type": "application/x-protobuf", "Content-Length": "0" }).end();
    });
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as { port: number };
  return {
    endpoint: `http://127.0.0.1:${port}`,
    seen,
    kill: () =>
      new Promise((resolve) => {
        for (const socket of sockets) socket.destroy();
        server.close(() => resolve());
      }),
  };
}

test("the agent keeps working when the collector is killed mid-run", async () => {
  const stand = await collector();
  const recorder = new Recorder({ agent: "email-agent", endpoint: stand.endpoint });
  const handler = new WakeCallbackHandler(recorder);

  await ask(emailAgentGraph(), handler);
  await recorder.flush();
  assert.ok(stand.seen.length > 0);
  assert.equal(recorder.health().sent, 4);

  await stand.kill();
  const started = performance.now();
  const result = await ask(emailAgentGraph(), handler);
  // The agent finishes without waiting on the dead collector.
  assert.ok(performance.now() - started < 500);
  assert.equal(result.messages.at(-1)?.content, ANSWER);

  await recorder.flush(10_000);
  const health = recorder.health();
  assert.equal(health.dropped, 4);
  assert.ok(!health.ok);
});

/** The app's own tracing, with a context manager registered globally, as an instrumented app has. */
class AppContextManager implements ContextManager {
  readonly #storage = new AsyncLocalStorage<Context>();
  active(): Context {
    return this.#storage.getStore() ?? ROOT_CONTEXT;
  }
  with<A extends unknown[], F extends (...args: A) => ReturnType<F>>(ctx: Context, fn: F, thisArg?: ThisParameterType<F>, ...args: A): ReturnType<F> {
    return this.#storage.run(ctx, () => fn.call(thisArg, ...args));
  }
  bind<T>(_ctx: Context, target: T): T {
    return target;
  }
  enable(): this {
    return this;
  }
  disable(): this {
    this.#storage.disable();
    return this;
  }
}

test("records under the app's unsampled span", async () => {
  // The app's own tracing left the current span unsampled (traceparent flags 00). Wake's
  // events don't join it, so they're recorded all the same, in a trace of their own.
  const { recorder, spans } = recorded("email-agent");
  const appTrace = "a0".padStart(32, "0");
  const appSpan = trace.wrapSpanContext({ traceId: appTrace, spanId: "b0".padStart(16, "0"), traceFlags: 0, isRemote: true });
  context.setGlobalContextManager(new AppContextManager());
  try {
    await context.with(trace.setSpan(context.active(), appSpan), () => ask(emailAgentGraph(), new WakeCallbackHandler(recorder)));
  } finally {
    context.disable();
  }
  const all = await spans();
  assert.ok(await recorder.flush());
  assert.equal(recorder.health().sent, 4);
  assert.ok(all.every((s) => s.spanContext().traceId !== appTrace && s.parentSpanContext?.traceId !== appTrace));
});

test("ignores the app's OTEL environment", async () => {
  const saved = { ...process.env };
  Object.assign(process.env, {
    OTEL_SDK_DISABLED: "true",
    OTEL_TRACES_SAMPLER: "always_off",
    OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT: "16",
    OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: "2",
    OTEL_BSP_MAX_QUEUE_SIZE: "1",
    OTEL_BSP_SCHEDULE_DELAY: "nonsense",
    OTEL_RESOURCE_ATTRIBUTES: "deployment.environment=app",
  });
  try {
    const { recorder, spans } = recorded("email-agent");
    await ask(emailAgentGraph(), new WakeCallbackHandler(recorder));
    const events = wakeEvents(await spans());
    assert.equal(recorder.health().sent, 4);
    assert.equal(events.length, 3);
    for (const event of events) {
      payload(event); // whole, not cut short
      assert.equal(attr(event, "wake.agent.id"), "email-agent");
    }
    assert.ok(!("deployment.environment" in events[0].resource.attributes));
  } finally {
    process.env = saved;
  }
});

test("counts spans that aren't recorded as dropped", async () => {
  const { recorder } = recorded("email-agent");
  (recorder.tracer._tracer as unknown as { _sampler: unknown })._sampler = new AlwaysOffSampler(); // however it happens, it can't go unnoticed
  await ask(emailAgentGraph(), new WakeCallbackHandler(recorder));
  assert.equal(await recorder.flush(), false);
  const health = recorder.health();
  assert.equal(health.sent, 0);
  assert.equal(health.dropped, 4);
  assert.ok(!health.ok);
});

test("the app's OTLP settings never reach the collector", async () => {
  const stand = await collector();
  const saved = { ...process.env };
  Object.assign(process.env, {
    OTEL_EXPORTER_OTLP_HEADERS: "authorization=Bearer app-secret",
    OTEL_EXPORTER_OTLP_TRACES_HEADERS: "x-api-key=app-key",
    OTEL_EXPORTER_OTLP_COMPRESSION: "gzip",
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "http://127.0.0.1:9/v1/traces",
    OTEL_EXPORTER_OTLP_TIMEOUT: "60",
  });
  try {
    const recorder = new Recorder({ agent: "email-agent", endpoint: stand.endpoint, headers: { "x-wake": "ours" } });
    emit(recorder, 1);
    assert.ok(await recorder.flush());
  } finally {
    process.env = saved;
    await stand.kill();
  }
  assert.equal(stand.seen.length, 1);
  const [[headers, body]] = stand.seen;
  assert.equal(headers["x-wake"], "ours");
  assert.equal(headers.authorization, undefined);
  assert.equal(headers["x-api-key"], undefined);
  assert.equal(headers["content-encoding"], undefined);
  assert.ok(!(body[0] === 0x1f && body[1] === 0x8b));
});

test("flush waits no longer than its timeout", async () => {
  // Accepts connections and never answers.
  const sockets: Socket[] = [];
  const hung = createTcpServer((socket) => sockets.push(socket));
  await new Promise<void>((resolve) => hung.listen(0, "127.0.0.1", resolve));
  const recorder = new Recorder({ agent: "email-agent", endpoint: `http://127.0.0.1:${(hung.address() as { port: number }).port}` });
  emit(recorder, 600); // more than one batch
  const started = performance.now();
  assert.equal(await recorder.flush(300), false);
  assert.ok(performance.now() - started < 1000);
  for (const socket of sockets) socket.destroy();
  hung.close();
});

test("flush says whether events were dropped", async () => {
  const exporter = new FailingExporter();
  const recorder = new Recorder({ agent: "email-agent", exporter });
  emit(recorder, 3);
  assert.equal(await recorder.flush(), false);
  exporter.failing = false;
  emit(recorder, 3);
  assert.equal(await recorder.flush(), true, "nothing dropped since the last flush");
});

test("counts events it failed to record", async () => {
  const drops: [number, string][] = [];
  const { recorder } = recorded("email-agent", { onDrop: (n, why) => drops.push([n, why]) });
  const handler = new WakeCallbackHandler(recorder);
  handler._startEvent = () => {
    throw new Error("recorder bug");
  };
  const result = await ask(emailAgentGraph(), handler);
  assert.equal(result.messages.at(-1)?.content, ANSWER);
  assert.equal(await recorder.flush(), false);
  const health = recorder.health();
  assert.equal(health.failed, 3); // two model calls and a tool call
  assert.equal(health.dropped, 0, "the collector got everything it was sent");
  assert.deepEqual(drops.map(([, why]) => why).sort(), ["failed to record handleChatModelStart", "failed to record handleToolStart"]);
  assert.equal(drops.reduce((sum, [n]) => sum + n, 0), 3);
});

test("a payload JSON can't hold is still recorded", async () => {
  const { recorder, spans } = recorded("email-agent");
  const handler = new WakeCallbackHandler(recorder);
  const loop: Record<string, unknown> = { name: "loop" };
  loop.self = loop;
  const lookup = tool(async () => ({ when: new Date(Date.UTC(2026, 9, 27)), big: 10n ** 20n, loop, two: 2 }) as unknown as string, {
    name: "lookup",
    description: "Look up an order.",
    schema: z.object({ order: z.string() }),
  });
  await lookup.invoke({ order: "1234" }, { callbacks: [handler] });
  assert.ok(await recorder.flush());
  const [event] = wakeEvents(await spans());
  assert.equal(attr(event, "wake.event.kind"), "tool_call");
  const output = payload(event).output as Record<string, unknown>;
  assert.equal(output.when, "2026-10-27T00:00:00.000Z");
  assert.equal(output.big, 100000000000000000000);
  assert.equal((output.loop as Record<string, unknown>).name, "loop");
  assert.equal(typeof (output.loop as Record<string, unknown>).self, "string");
  assert.equal(recorder.health().failed, 0);
});
