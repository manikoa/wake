/** The recorder: sends events to the Wake collector without ever blocking the agent. */

import { readFileSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

import { context as otelContext, ROOT_CONTEXT, type Context, type SpanOptions, type Span } from "@opentelemetry/api";
import { ExportResultCode, suppressTracing, type ExportResult } from "@opentelemetry/core";
import { ProtobufTraceSerializer } from "@opentelemetry/otlp-transformer";
import { resourceFromAttributes } from "@opentelemetry/resources";
import {
  AlwaysOnSampler,
  TracerProvider,
  type ReadableSpan,
  type SpanExporter,
  type SpanProcessor,
} from "@opentelemetry/sdk-trace";

import { AGENT_ID } from "./events.ts";
import { log } from "./log.ts";

export const DEFAULT_ENDPOINT = "http://localhost:4318";
// How long one export may take. Short, so a hung collector costs a flush little; a batch
// that times out is dropped and counted.
const EXPORT_TIMEOUT_MILLIS = 3000;
const SCHEDULE_DELAY_MILLIS = 5000;
const MAX_EXPORT_BATCH_SIZE = 512;

export type { SpanExporter };

// OpenTelemetry stamps a span with Date.now(), to the millisecond, and events a millisecond
// apart would lose their order. Wake's spans are stamped from this clock instead: wall time
// when the process started, plus monotonic time since.
const anchor = Date.now() - performance.now();

/** Now, in milliseconds since the epoch, finer than a millisecond. */
export function now(): number {
  return anchor + performance.now();
}

/**
 * What the recorder has sent and dropped since it started. failed counts the events it
 * couldn't record at all, such as one whose hook threw.
 */
export interface Health {
  readonly sent: number;
  readonly dropped: number;
  readonly lastError: string | null;
  readonly failed: number;
  readonly ok: boolean;
}

export interface RecorderOptions {
  /** The agent the events are recorded for: wake.agent.id. */
  agent: string;
  /** The collector's OTLP/HTTP endpoint. Defaults to WAKE_ENDPOINT, then http://localhost:4318. */
  endpoint?: string;
  /** Headers sent with each export. */
  headers?: Record<string, string>;
  /** The CA certificate that signed the collector's TLS certificate. Defaults to WAKE_CA_FILE. */
  caFile?: string;
  /** Where spans go instead of the collector, such as an in-memory exporter in tests. */
  exporter?: SpanExporter;
  maxQueueSize?: number;
  /** Called with each count of events dropped and why, after the export that dropped them. */
  onDrop?: (count: number, reason: string) => void;
}

/**
 * Counts sent, dropped and failed events.
 *
 * Drops and failures are counted wherever they happen, including inside the agent's own
 * calls. Reporting them, the warning and the onDrop call, happens only when spans are
 * exported or flushed, never inside a call the agent made. A slow or failing onDrop can
 * never slow or break the agent.
 */
class Counts {
  sent = 0;
  dropped = 0;
  failed = 0;
  lastError: string | null = null;
  #unreported = new Map<string, number>();
  #warned = false;
  readonly #onDrop?: (count: number, reason: string) => void;

  constructor(onDrop?: (count: number, reason: string) => void) {
    this.#onDrop = onDrop;
  }

  snapshot(): Health {
    return { sent: this.sent, dropped: this.dropped, lastError: this.lastError, failed: this.failed, ok: this.lastError === null };
  }

  lost(): number {
    return this.dropped + this.failed;
  }

  recordSent(count: number): void {
    this.sent += count;
    this.lastError = null;
  }

  recordDropped(count: number, reason: string): void {
    this.dropped += count;
    this.#lose(count, reason);
  }

  recordFailed(reason: string): void {
    this.failed += 1;
    this.#lose(1, reason);
  }

  #lose(count: number, reason: string): void {
    this.lastError = reason;
    this.#unreported.set(reason, (this.#unreported.get(reason) ?? 0) + count);
  }

  /** Warns and calls onDrop for drops since the last report. */
  report(): void {
    const pending = this.#unreported;
    this.#unreported = new Map();
    const recovered = pending.size === 0 && this.#warned && this.lastError === null;
    const started = pending.size > 0 && !this.#warned;
    if (pending.size > 0) this.#warned = true;
    else if (recovered) this.#warned = false;
    if (started) log.warn(`dropping events (${[...pending.keys()].join(", ")}); the agent keeps working`);
    if (recovered) log.warn("collector reachable again, recording resumed");
    if (this.#onDrop === undefined) return;
    for (const [reason, count] of pending) {
      try {
        this.#onDrop(count, reason);
      } catch (error) {
        log.warn("onDrop callback failed", error);
      }
    }
  }
}

/**
 * Sends spans to the collector as OTLP/HTTP protobuf, configured only by its arguments.
 *
 * The app's OTEL_EXPORTER_OTLP_* settings are for its own telemetry, and its headers can
 * hold its secrets, so none of them apply here. Each batch is tried once, with a short
 * timeout: a batch that fails is dropped and counted, and the next one goes on. The
 * request is made with tracing suppressed, so the app's own HTTP instrumentation, if it
 * has any, doesn't record Wake's exports.
 */
class OTLPExporter implements SpanExporter {
  readonly #url: URL;
  readonly #headers: Record<string, string>;
  readonly #ca?: Buffer;
  readonly #timeout: number;

  constructor(endpoint: string, headers: Record<string, string> | undefined, caFile: string | undefined, timeout: number) {
    this.#url = new URL(`${endpoint.replace(/\/+$/, "")}/v1/traces`);
    this.#headers = { ...headers, "Content-Type": "application/x-protobuf" };
    this.#ca = caFile ? readFileSync(caFile) : undefined;
    this.#timeout = timeout;
  }

  export(spans: ReadableSpan[], done: (result: ExportResult) => void): void {
    let body: Uint8Array | undefined;
    try {
      body = ProtobufTraceSerializer.serializeRequest(spans);
    } catch (error) {
      done({ code: ExportResultCode.FAILED, error: error as Error });
      return;
    }
    const send = this.#url.protocol === "https:" ? httpsRequest : httpRequest;
    otelContext.with(suppressTracing(otelContext.active()), () => {
      const request = send(
        this.#url,
        {
          method: "POST",
          headers: { ...this.#headers, "Content-Length": String(body?.byteLength ?? 0) },
          timeout: this.#timeout,
          ...(this.#ca ? { ca: this.#ca } : {}),
        },
        (response) => {
          response.resume();
          response.on("end", () => {
            const status = response.statusCode ?? 0;
            if (status >= 200 && status < 300) done({ code: ExportResultCode.SUCCESS });
            else done({ code: ExportResultCode.FAILED, error: new Error(`the collector answered HTTP ${status}`) });
          });
          response.on("error", (error) => done({ code: ExportResultCode.FAILED, error }));
        },
      );
      request.on("timeout", () => request.destroy(new Error(`no answer from the collector in ${this.#timeout}ms`)));
      request.on("error", (error) => done({ code: ExportResultCode.FAILED, error }));
      request.end(body);
    });
  }

  async shutdown(): Promise<void> {}

  async forceFlush(): Promise<void> {}
}

/**
 * Holds ended spans in a bounded queue and exports them in batches, from timers and
 * completions, never inside the agent's call that ended the span. A span arriving at a
 * full queue pushes out the oldest one, which is counted as dropped. Exports go one at a
 * time; each is counted as sent or dropped, then drops are reported.
 */
class QueueProcessor implements SpanProcessor {
  readonly #queue: ReadableSpan[] = [];
  readonly #exporter: SpanExporter;
  readonly #counts: Counts;
  readonly #maxQueueSize: number;
  readonly #batchSize: number;
  #running: Promise<void> | null = null;
  #all = false;
  #timer: NodeJS.Timeout | null = null;
  #closed = false;

  constructor(exporter: SpanExporter, counts: Counts, maxQueueSize: number) {
    this.#exporter = exporter;
    this.#counts = counts;
    this.#maxQueueSize = Math.max(1, maxQueueSize);
    this.#batchSize = Math.min(MAX_EXPORT_BATCH_SIZE, this.#maxQueueSize);
  }

  onStart(): void {}

  onEnd(span: ReadableSpan): void {
    if (this.#closed) {
      this.#counts.recordDropped(1, "recorder shut down");
      return;
    }
    if (this.#queue.length >= this.#maxQueueSize) {
      this.#queue.shift();
      this.#counts.recordDropped(1, "queue full");
    }
    this.#queue.push(span);
    queued.add(this);
    if (this.#queue.length >= this.#batchSize && this.#running === null) {
      // Started on the next turn of the event loop, so an exporter that answers at once
      // can't run onDrop inside the agent's call.
      setImmediate(() => void this.#drain(false));
    } else {
      this.#schedule();
    }
  }

  /** Exports everything queued, including what's queued while it runs. */
  async forceFlush(): Promise<void> {
    while (this.#queue.length > 0 || this.#running !== null) {
      await this.#drain(true);
    }
  }

  async shutdown(): Promise<void> {
    await this.forceFlush();
    this.#closed = true;
    if (this.#timer !== null) clearTimeout(this.#timer);
    await this.#exporter.shutdown();
  }

  #schedule(): void {
    if (this.#timer !== null || this.#running !== null) return;
    this.#timer = setTimeout(() => {
      this.#timer = null;
      void this.#drain(true);
    }, SCHEDULE_DELAY_MILLIS);
    this.#timer.unref();
  }

  #drain(all: boolean): Promise<void> {
    if (all) this.#all = true;
    if (this.#running !== null) return this.#running;
    if (this.#timer !== null) {
      clearTimeout(this.#timer);
      this.#timer = null;
    }
    this.#running = (async () => {
      try {
        while (this.#queue.length > 0 && (this.#all || this.#queue.length >= this.#batchSize)) {
          await this.#export(this.#queue.splice(0, this.#batchSize));
        }
      } finally {
        this.#all = false;
        this.#running = null;
        if (this.#queue.length > 0) this.#schedule();
        else queued.delete(this);
      }
    })();
    return this.#running;
  }

  #export(batch: ReadableSpan[]): Promise<void> {
    return new Promise((resolve) => {
      let settled = false;
      const settle = (result: ExportResult) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (result.code === ExportResultCode.SUCCESS) this.#counts.recordSent(batch.length);
        else this.#counts.recordDropped(batch.length, result.error ? `export failed: ${result.error.message}` : "export failed");
        this.#counts.report();
        resolve();
      };
      const timer = setTimeout(() => settle({ code: ExportResultCode.FAILED, error: new Error("timed out") }), 30_000);
      timer.unref();
      try {
        this.#exporter.export(batch, settle);
      } catch (error) {
        settle({ code: ExportResultCode.FAILED, error: error as Error });
      }
    });
  }
}

/**
 * Wake's tracer. A span with no Wake parent starts a trace of its own, instead of joining
 * whatever span the app has active, which the app's sampler may have left unsampled. A
 * span that isn't recorded anyway is counted as dropped, so health() can't stay ok while
 * nothing is recorded. Spans start and end on Wake's finer clock, unless given times.
 */
export class WakeTracer {
  readonly _tracer: ReturnType<TracerProvider["getTracer"]>;
  readonly #counts: Counts;

  constructor(tracer: ReturnType<TracerProvider["getTracer"]>, counts: Counts) {
    this._tracer = tracer;
    this.#counts = counts;
  }

  startSpan(name: string, options: SpanOptions = {}, context?: Context): Span {
    const span = this._tracer.startSpan(name, { ...options, startTime: options.startTime ?? now() }, context ?? ROOT_CONTEXT);
    if (!span.isRecording()) this.#counts.recordDropped(1, "span not recorded");
    const end = span.end.bind(span);
    span.end = (endTime) => end(endTime ?? now());
    return span;
  }
}

/**
 * Owns a private OpenTelemetry pipeline, so it never touches the app's own tracing setup:
 * nothing is registered globally.
 *
 * Spans are exported from a bounded queue in the background. If the collector is slow or
 * down, events are dropped and counted, and the agent keeps working. Check health(), or
 * pass onDrop to page someone. The recorder holds no signing keys: the collector signs
 * events on arrival (see docs/signing.md).
 *
 * Its settings come from its options and WAKE_* variables only: WAKE_ENDPOINT, and
 * WAKE_CA_FILE for the CA that signed the collector's TLS certificate. The app's OTEL_*
 * variables configure the app's telemetry, not Wake's. What's still queued is sent when
 * the process has nothing left to do, or on shutdown().
 */
export class Recorder {
  readonly agent: string;
  readonly tracer: WakeTracer;
  readonly #counts: Counts;
  readonly #provider: TracerProvider;
  readonly #processor: QueueProcessor;
  #flushedLost = 0;
  #shutdown: Promise<void> | null = null;

  constructor(options: RecorderOptions) {
    this.agent = options.agent;
    const exporter =
      options.exporter ??
      new OTLPExporter(
        options.endpoint || process.env.WAKE_ENDPOINT || DEFAULT_ENDPOINT,
        options.headers,
        options.caFile || process.env.WAKE_CA_FILE || undefined,
        EXPORT_TIMEOUT_MILLIS,
      );
    this.#counts = new Counts(options.onDrop);
    this.#processor = new QueueProcessor(exporter, this.#counts, options.maxQueueSize ?? 2048);
    // Every event is recorded: every setting is given, and attribute values aren't cut
    // short, since wake.payload must stay valid JSON.
    this.#provider = new TracerProvider({
      sampler: new AlwaysOnSampler(),
      resource: resourceFromAttributes({ "service.name": this.agent, [AGENT_ID]: this.agent }),
      spanLimits: {
        attributeCountLimit: 128,
        attributeValueLengthLimit: Infinity,
        eventCountLimit: 128,
        linkCountLimit: 128,
        attributePerEventCountLimit: 128,
        attributePerLinkCountLimit: 128,
      },
      spanProcessors: [this.#processor],
    });
    this.tracer = new WakeTracer(this.#provider.getTracer("wake"), this.#counts);
  }

  health(): Health {
    return this.#counts.snapshot();
  }

  /**
   * Sends what's queued, waiting at most timeoutMillis. Resolves false if that ran out, or
   * if any event was dropped or failed since the last flush.
   */
  async flush(timeoutMillis = 5000): Promise<boolean> {
    let timer: NodeJS.Timeout | undefined;
    const finished = await Promise.race([
      this.#processor.forceFlush().then(
        () => true,
        (error) => {
          log.warn("flush failed", error);
          return true;
        },
      ),
      new Promise<false>((resolve) => {
        timer = setTimeout(() => resolve(false), timeoutMillis);
      }),
    ]);
    clearTimeout(timer);
    this.#counts.report();
    const lost = this.#counts.lost();
    const clean = lost === this.#flushedLost;
    this.#flushedLost = lost;
    return clean && finished;
  }

  /** Sends what's queued and stops. Events recorded after it are dropped. */
  shutdown(): Promise<void> {
    this.#shutdown ??= this.#provider.shutdown().catch((error) => log.warn("shutdown failed", error));
    return this.#shutdown;
  }

  /** Counts an event an integration couldn't record. */
  _failed(what: string): void {
    this.#counts.recordFailed(`failed to record ${what}`);
  }
}

// What's still queued is sent when the process runs out of work, as Python's OpenTelemetry
// SDK does at exit. Only processors holding spans are kept here.
const queued = new Set<QueueProcessor>();
process.on("beforeExit", () => {
  for (const processor of queued) void processor.forceFlush();
});
