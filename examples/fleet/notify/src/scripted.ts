/**
 * The notify agent's scripted model, so the fleet runs anywhere with no API keys. Like the
 * fleet's other scripted models it does what its input says: it emails the customer what
 * it was asked to, notes that in its outbox, then answers. One step per tool result.
 */

import { randomBytes } from "node:crypto";

import { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import type { ChatResult } from "@langchain/core/outputs";

const ADDRESS = /[\w.+-]+@[\w-]+(\.[\w-]+)+/;

function text(content: unknown): string {
  return typeof content === "string" ? content : JSON.stringify(content);
}

export class NotifyModel extends BaseChatModel {
  readonly #sendTool: string;

  /** sendTool is the email tool's name, as the gateway lists it. */
  constructor(sendTool: string) {
    super({});
    this.#sendTool = sendTool;
  }

  _llmType(): string {
    return "scripted-notify";
  }

  async _generate(messages: BaseMessage[]): Promise<ChatResult> {
    const request = text(messages.find((m) => m.type === "human")?.content);
    const results = messages.filter((m) => m.type === "tool");
    const last = results.length > 0 ? text(results.at(-1)!.content) : "";
    const to = ADDRESS.exec(request)?.[0] ?? "customer@example.com";
    const body = request.replace(/^.*?:\s*/, "");
    const steps = [
      [this.#sendTool, { to, subject: "Your refund", body }],
      ["remember", { note: `Emailed ${to}: ${body}` }],
    ] as const;
    const message =
      results.length < steps.length
        ? new AIMessage({ content: "", tool_calls: [{ name: steps[results.length][0], args: steps[results.length][1], id: `call_${randomBytes(4).toString("hex")}` }] })
        : new AIMessage(`Emailed ${to}. ${last}`);
    return { generations: [{ message, text: text(message.content) }] };
  }
}
