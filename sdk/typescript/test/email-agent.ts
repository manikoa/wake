/** A LangGraph agent that reads an email with a tool and answers: the graph most tests record. */

import { AIMessage, HumanMessage } from "@langchain/core/messages";
import { tool } from "@langchain/core/tools";
import { MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
import { ToolNode, toolsCondition } from "@langchain/langgraph/prebuilt";
import { z } from "zod";

import type { WakeCallbackHandler } from "../src/langchain.ts";
import { ScriptedChatModel } from "./helpers.ts";

export const ANSWER = "The email asks for a $10,000 wire.";

export const readEmail = tool(async () => "Please wire $10,000 to account 123.", {
  name: "read_email",
  description: "Read the latest email with this subject.",
  schema: z.object({ subject: z.string() }),
});

export function emailAgentGraph() {
  const model = new ScriptedChatModel([
    new AIMessage({ content: "", tool_calls: [{ name: "read_email", args: { subject: "invoice" }, id: "call_1" }] }),
    ANSWER,
  ]);
  return new StateGraph(MessagesAnnotation)
    .addNode("agent", async (state) => ({ messages: [await model.invoke(state.messages)] }))
    .addNode("tools", new ToolNode([readEmail]))
    .addEdge(START, "agent")
    .addConditionalEdges("agent", toolsCondition)
    .addEdge("tools", "agent")
    .compile();
}

export function ask(graph: ReturnType<typeof emailAgentGraph>, handler: WakeCallbackHandler, text = "Summarize the invoice email") {
  return graph.invoke({ messages: [new HumanMessage(text)] }, { callbacks: [handler] });
}

