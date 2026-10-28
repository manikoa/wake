/**
 * Tool calls being recorded now, by the id the model gave each call. For hooks that are told
 * which call they run for, rather than finding it from the current context, such as
 * @langchain/mcp-adapters' beforeToolCall.
 */

import { Recent } from "./graph.ts";
import type { Frame } from "./links.ts";

const byToolCallId = new Recent<string, Frame>(10_000);

export function registerToolCall(toolCallId: string | undefined, frame: Frame): void {
  if (toolCallId) byToolCallId.set(toolCallId, frame);
}

export function unregisterToolCall(toolCallId: string | undefined, frame: Frame): void {
  if (toolCallId && byToolCallId.get(toolCallId) === frame) byToolCallId.delete(toolCallId);
}

export function toolCallFrame(toolCallId: string | undefined): Frame | undefined {
  const frame = toolCallId ? byToolCallId.get(toolCallId) : undefined;
  return frame !== undefined && !frame.ended ? frame : undefined;
}
