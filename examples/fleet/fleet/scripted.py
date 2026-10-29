"""Scripted stand-ins for the fleet's three models, so it runs anywhere with no API keys.
Each behaves as a model too willing to follow what it reads would: it does what its
input says, which is how the poisoned page gets a payment out of billing.

Each plugs in where its framework takes a model, and reports itself the way the real
one does, so Wake records exactly what it would with the real models.
"""

import asyncio
import json
import re
import uuid
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tracing import response_span
from agents.usage import Usage
from claude_agent_sdk import Transport
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

POLICY_URL = "https://shop.example.com/refund-policy"
# Each refund a request asks for: "refund $80 for order 1234", "refund $500 to account 999-1234".
REFUND = re.compile(r"refund \$(\d+(?:\.\d+)?)(?: (?:for|on) order (\d+)| to account ([\w-]+))", re.IGNORECASE)


class SupportModel(BaseChatModel):
    """The support agent's model: look the order up, ask research about the policy, ask
    billing to refund, handing it the order record, note what happened, ask notify to email
    the customer what billing said, then answer. One step per tool result."""

    lookup_tool: str = "lookup_order"  # the CRM tool's name, as the gateway lists it

    @property
    def _llm_type(self) -> str:
        return "scripted-support"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        results = [m for m in messages if isinstance(m, ToolMessage)]
        last = results[-1].content if results else ""
        order = results[0].content if results else ""
        billed = results[2].content if len(results) > 2 else ""
        steps = [
            (self.lookup_tool, {"order": "1234"}),
            ("ask_research", {"question": f"What does our refund policy at {POLICY_URL} say?"}),
            # It hands billing the whole order record, as agents pass their context along:
            # what the CRM said goes wherever the request does.
            ("ask_billing", {"request": f"Please refund $80 for order 1234 ({order}). Research says: {last}"}),
            ("remember", {"note": f"Order 1234 refund handled. Billing says: {last}"}),
            ("ask_notify", {"request": f"Email jane@example.com: {billed}"}),
        ]
        if len(results) < len(steps):
            name, args = steps[len(results)]
            message = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}"}])
        else:
            message = AIMessage(content=f"Done. {last}")
        return ChatResult(generations=[ChatGeneration(message=message)])


class ResearchModel(Model):
    """The research agent's model: fetch the page the question names, then report what it
    says. Reports each response to the Agents SDK's tracing as its Responses model does."""

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
                           previous_response_id=None, conversation_id=None, prompt=None, **kwargs):
        items = input if isinstance(input, list) else [{"role": "user", "content": input}]
        outputs = [i for i in items if _field(i, "type") == "function_call_output"]
        if not outputs:
            url = re.search(r"https?://\S+?(?=[\s?]|$)", json.dumps(items, default=str))
            arguments = json.dumps({"url": url.group(0) if url else POLICY_URL})
            output = [ResponseFunctionToolCall(type="function_call", id=f"fc_{uuid.uuid4().hex[:8]}",
                                               call_id=f"call_{uuid.uuid4().hex[:8]}", name="fetch_page",
                                               arguments=arguments, status="completed")]
        else:
            page = _field(outputs[-1], "output")
            output = [ResponseOutputMessage(type="message", id=f"msg_{uuid.uuid4().hex[:8]}", role="assistant",
                                            status="completed", content=[ResponseOutputText(
                                                type="output_text", annotations=[],
                                                text=f"The policy page says: {page}")])]
        response_id = f"resp_{uuid.uuid4().hex[:8]}"
        with response_span(disabled=tracing.is_disabled()) as span:
            span.span_data.response = Response.model_construct(id=response_id, model="scripted-research",
                                                               output=output, object="response")
            span.span_data.input = input
        return ModelResponse(output=output, usage=Usage(), response_id=response_id)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


class ClaudeCode(Transport):
    """Stands in for the Claude Code CLI under the Claude Agent SDK. It plays one turn over
    the SDK's control protocol, as Claude Code does: issue every refund the prompt asks for
    with the in-process tool, then confirm. So the SDK's own code parses each message,
    calls Wake's hooks and runs the tool."""

    def __init__(self, prompt: str, server: str, tool: str) -> None:
        self._prompt = prompt
        self._server = server
        self._tool = tool
        self._out: asyncio.Queue = asyncio.Queue()
        self._waiting: dict[str, asyncio.Future] = {}
        self._hooks: dict[str, list[str]] = {}
        self._requests = 0
        self._task: asyncio.Task | None = None

    async def connect(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            message = json.loads(line)
            if message["type"] == "control_request" and message["request"]["subtype"] == "initialize":
                for event, matchers in (message["request"].get("hooks") or {}).items():
                    self._hooks[event] = [i for m in matchers for i in m["hookCallbackIds"]]
                await self._emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": message["request_id"], "response": {}}})
            elif message["type"] == "control_response":
                self._waiting.pop(message["response"]["request_id"]).set_result(message["response"])
            elif message["type"] == "user" and self._task is None:
                self._task = asyncio.create_task(self._turn())

    async def end_input(self) -> None:
        await self._out.put(None)

    async def close(self) -> None:
        if self._task is not None:
            await self._task

    async def read_messages(self):
        while (message := await self._out.get()) is not None:
            yield message

    async def _turn(self) -> None:
        session = f"session-{uuid.uuid4().hex[:8]}"
        await self._emit({"type": "system", "subtype": "init", "session_id": session, "uuid": uuid.uuid4().hex})
        refunds = [{"amount": float(m[1]), **({"order": m[2]} if m[2] else {"account": m[3]})}
                   for m in REFUND.finditer(self._prompt)]
        name = f"mcp__{self._server}__{self._tool}"
        uses = [(f"toolu_{uuid.uuid4().hex[:12]}", r) for r in refunds]
        blocks = [{"type": "text", "text": f"Issuing {len(uses)} refund(s)."}]
        blocks += [{"type": "tool_use", "id": i, "name": name, "input": r} for i, r in uses]
        await self._assistant(session, "msg_1", blocks)
        if uses:
            await self._mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "claude-code", "version": "2"}}, 1)
            await self._mcp("notifications/initialized", {}, None)
        results = []
        for n, (tool_use_id, arguments) in enumerate(uses):
            hook = {"tool_name": name, "tool_input": arguments, "session_id": session}
            await self._hook("PreToolUse", tool_use_id, hook)
            response = await self._mcp("tools/call", {"name": self._tool, "arguments": arguments}, n + 2)
            text = "\n".join(c.get("text", "") for c in response.get("result", {}).get("content", []))
            await self._hook("PostToolUse", tool_use_id, {**hook, "tool_response": text})
            results.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": text})
        if results:
            await self._emit({"type": "user", "parent_tool_use_id": None, "session_id": session,
                              "message": {"role": "user", "content": results}})
        answer = " ".join(r["content"] for r in results) or "Nothing to refund."
        await self._assistant(session, "msg_2", [{"type": "text", "text": answer}])
        await self._emit({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
                          "is_error": False, "num_turns": 2, "session_id": session, "result": answer})

    async def _assistant(self, session: str, message_id: str, blocks: list) -> None:
        # Claude Code sends each content block of a response as its own message.
        for block in blocks:
            await self._emit({"type": "assistant", "parent_tool_use_id": None, "session_id": session,
                              "message": {"id": message_id, "model": "scripted-billing", "content": [block]}})

    async def _hook(self, event: str, tool_use_id: str, fields: dict) -> None:
        for callback_id in self._hooks.get(event, []):
            await self._request({"subtype": "hook_callback", "callback_id": callback_id, "tool_use_id": tool_use_id,
                                 "input": {"hook_event_name": event, "transcript_path": "", "cwd": "/",
                                           "tool_use_id": tool_use_id, **fields}})

    async def _mcp(self, method: str, params: dict, request_id: int | None) -> dict:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        response = await self._request({"subtype": "mcp_message", "server_name": self._server, "message": message})
        return response.get("mcp_response") or {}

    async def _request(self, request: dict) -> dict:
        self._requests += 1
        request_id = f"cli_{self._requests}"
        self._waiting[request_id] = asyncio.get_running_loop().create_future()
        await self._emit({"type": "control_request", "request_id": request_id, "request": request})
        response = await self._waiting[request_id]
        return response.get("response") or {}

    async def _emit(self, message: dict) -> None:
        await self._out.put(message)
