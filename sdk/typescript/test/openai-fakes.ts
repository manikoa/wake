/** A fake OpenAI model that reports its responses to tracing the way the SDK's own models do. */

import { randomBytes } from "node:crypto";

import { Usage, withResponseSpan, type Model, type ModelRequest, type ModelResponse } from "@openai/agents";

export type ApiItem = Record<string, unknown>;

export function call(name: string, args: string, callId: string): ApiItem {
  return { type: "function_call", id: `fc_${callId}`, call_id: callId, name, arguments: args, status: "completed" };
}

export function say(text: string): ApiItem {
  return { type: "message", id: `msg_${randomBytes(4).toString("hex")}`, role: "assistant", status: "completed", content: [{ type: "output_text", text, annotations: [] }] };
}

/** A web search OpenAI ran inside the model call, as the Responses API reports it. */
export function searched(query: string): ApiItem {
  return { type: "web_search_call", id: `ws_${randomBytes(4).toString("hex")}`, status: "completed", action: { type: "search", query } };
}

/** A hosted MCP server's request to call one of its tools, as the Responses API reports it. */
export function mcpRequest(server: string, name: string, args: string, id = `mcpr_${randomBytes(4).toString("hex")}`): ApiItem {
  return { type: "mcp_approval_request", id, server_label: server, name, arguments: args };
}

/** A call to a hosted MCP server's tool OpenAI made inside the model call, as the Responses API reports it. */
export function mcpCall(server: string, name: string, output: string): ApiItem {
  return { type: "mcp_call", id: `mcp_${randomBytes(4).toString("hex")}`, server_label: server, name, arguments: "{}", output };
}

/** Item types a hosted tool reports its work by, which the SDK keeps as hosted tool calls. */
const HOSTED = new Set(["web_search_call", "file_search_call", "code_interpreter_call", "image_generation_call", "mcp_call", "mcp_approval_request", "mcp_list_tools"]);

/** The Responses API's output items as the SDK's own items, as its Responses model converts them. */
export function asItems(output: ApiItem[]): ModelResponse["output"] {
  return output.map((item) => {
    if (item.type === "function_call") return { type: "function_call", id: item.id, callId: item.call_id, name: item.name, arguments: item.arguments, status: "completed" };
    if (HOSTED.has(item.type as string)) {
      const { output, status, ...providerData } = item;
      return { type: "hosted_tool_call", id: item.id, name: item.type, status: status ?? "completed", output, providerData };
    }
    return { type: "message", id: item.id, role: "assistant", status: "completed", content: (item.content as { text: string }[]).map((c) => ({ type: "output_text", text: c.text })) };
  }) as ModelResponse["output"];
}

/** Answers each turn from a script, and reports it as the SDK's Responses model does. */
export class FakeModel implements Model {
  readonly #turns: ApiItem[][];

  constructor(turns: ApiItem[][]) {
    this.#turns = turns;
  }

  async getResponse(request: ModelRequest): Promise<ModelResponse> {
    const output = this.#turns.shift()!;
    const responseId = `resp_${randomBytes(4).toString("hex")}`;
    const answer = { output: asItems(output), usage: new Usage(), responseId } as unknown as ModelResponse;
    if (!request.tracing) return answer;
    return withResponseSpan(async (span) => {
      // As the SDK's own model does, nothing of the exchange without sensitive data.
      if (request.tracing === true) {
        span.spanData.response_id = responseId;
        span.spanData._input = request.input as never;
        span.spanData._response = { id: responseId, model: "fake-model", output, object: "response" };
      }
      return answer;
    });
  }

  async *getStreamedResponse(): AsyncIterable<never> {
    throw new Error("not streamed");
  }
}

