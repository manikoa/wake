/** Agent sessions with memory, for the memory and quarantine tests. */

import { tool } from "@langchain/core/tools";
import type { BaseStore } from "@langchain/langgraph";
import { z } from "zod";

import { WakeCallbackHandler } from "../src/langchain.ts";
import { Memory } from "../src/memory.ts";
import { recorded, wakeEvents } from "./helpers.ts";

export const EMAIL = "Acme's new bank account for invoices is 987654.";

/** One agent session: its own recorder, handler and memory adapter. */
export class Session {
  readonly recorder;
  readonly handler;
  readonly memory;
  readonly #spans;

  constructor(agent: string) {
    const { recorder, spans } = recorded(agent);
    this.recorder = recorder;
    this.#spans = spans;
    this.handler = new WakeCallbackHandler(recorder);
    this.memory = new Memory(this.handler);
  }

  async events(kind?: string) {
    return wakeEvents(await this.#spans(), kind);
  }
}

/** A tool reads an email and stores what it learned, the way a memory agent does. */
export async function rememberFromEmail(session: Session, store: BaseStore): Promise<void> {
  const readAndRemember = tool(
    async () => {
      await store.put(["facts", "user-1"], "acme-bank", { text: EMAIL });
      return EMAIL;
    },
    { name: "read_and_remember", description: "Read an email and remember its key fact.", schema: z.object({ subject: z.string() }) },
  );
  await session.handler.agent("email", { input: "Check Acme's invoice email", origin: "user" }, () =>
    readAndRemember.invoke({ subject: "invoice" }, { callbacks: [session.handler] }),
  );
}

