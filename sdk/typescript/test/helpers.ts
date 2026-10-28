/** What the tests share: a recorder whose spans stay in memory, and ways to look at them. */

import { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import type { ChatResult } from "@langchain/core/outputs";
import { InMemorySpanExporter, type ReadableSpan } from "@opentelemetry/sdk-trace";

import { Recorder, type RecorderOptions } from "../src/index.ts";

export type Spans = () => Promise<ReadableSpan[]>;

/** A recorder for this agent that keeps its spans, and a way to read them after a flush. */
export function recorded(agent: string, options: Partial<RecorderOptions> = {}): { recorder: Recorder; exporter: InMemorySpanExporter; spans: Spans } {
  const exporter = new InMemorySpanExporter();
  const recorder = new Recorder({ agent, exporter, ...options });
  const spans = async () => {
    await recorder.flush();
    return exporter.getFinishedSpans();
  };
  return { recorder, exporter, spans };
}

function nanos(span: ReadableSpan): bigint {
  return BigInt(span.startTime[0]) * 1_000_000_000n + BigInt(span.startTime[1]);
}

/** The spans that are Wake events, in the order they started. */
export function wakeEvents(spans: ReadableSpan[], kind?: string): ReadableSpan[] {
  const found = spans
    .map((span, i) => ({ span, i }))
    .filter(({ span }) => "wake.event.kind" in span.attributes && (kind === undefined || span.attributes["wake.event.kind"] === kind));
  found.sort((a, b) => (nanos(a.span) < nanos(b.span) ? -1 : nanos(a.span) > nanos(b.span) ? 1 : a.i - b.i));
  return found.map(({ span }) => span);
}

export function attr(span: ReadableSpan, key: string): unknown {
  return span.attributes[key];
}

export function id(span: ReadableSpan): string {
  return span.attributes["wake.event.id"] as string;
}

export function ids(...spans: ReadableSpan[]): Set<string> {
  return new Set(spans.map(id));
}

export function parents(span: ReadableSpan): string[] {
  return [...((span.attributes["wake.parents"] as string[] | undefined) ?? [])];
}

export function origins(span: ReadableSpan): string[] {
  return [...((span.attributes["wake.origins"] as string[] | undefined) ?? [])];
}

export function payload(span: ReadableSpan): Record<string, unknown> {
  return JSON.parse(span.attributes["wake.payload"] as string);
}

export function parentSpanId(span: ReadableSpan): string | undefined {
  return span.parentSpanContext?.spanId;
}

/** A chat model that answers with these messages, one per call, as Python's GenericFakeChatModel does. */
export class ScriptedChatModel extends BaseChatModel {
  readonly #replies: AIMessage[];

  constructor(replies: (AIMessage | string)[]) {
    super({});
    this.#replies = replies.map((r) => (typeof r === "string" ? new AIMessage(r) : r));
  }

  _llmType(): string {
    return "scripted";
  }

  async _generate(_messages: BaseMessage[]): Promise<ChatResult> {
    const reply = this.#replies.shift();
    if (reply === undefined) throw new Error("no more scripted replies");
    // A fresh message each call, as a model returns one.
    const message = new AIMessage({ content: reply.content, tool_calls: reply.tool_calls });
    return { generations: [{ message, text: typeof reply.content === "string" ? reply.content : "" }] };
  }
}

/** Waits for the timers and I/O the recorder schedules to run. */
export function tick(ms = 0): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
