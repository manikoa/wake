"""The deny lever end to end, for smoke_test.sh: the brake halts one agent, by name, and
that agent's own outbound MCP call through agentgateway is denied, naming the halt, while
another agent's isn't. A caller with no SVID never gets past the gateway's handshake.

    python deny_check.py <dir with ca.pem, brake.pem/.key, agents/<name>.pem/.key>

Standard library only, so it runs on any python3.
"""

import http.client
import json
import ssl
import sys
import urllib.error
import urllib.request

work = sys.argv[1]
CONTROL = "https://127.0.0.1:9002"
GATEWAY = "https://localhost:3000/mcp"
HALT = "smoke-halt"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "deny-check", "version": "0"}},
}


def context(identity: str | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=f"{work}/ca.pem")
    if identity:
        ctx.load_cert_chain(f"{work}/{identity}.pem", f"{work}/{identity}.key")
    return ctx


def call(ctx: ssl.SSLContext, method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={"content-type": "application/json", "accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, context=ctx, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# The control API's certificate is the proxy's SVID: a SPIFFE ID, no host name. The chain
# is checked; the name isn't.
brake = context("brake")
brake.check_hostname = False

status, _, body = call(brake, "GET", f"{CONTROL}/v1/halts")
state = json.loads(body)
assert status == 200 and state.get("epoch") and not state.get("uninitialized"), (status, state)

status, _, body = call(brake, "PUT", f"{CONTROL}/v1/halts/{HALT}", {"scope": {"agents": ["smoke-test"]}})
assert status == 200, (status, body)
print(f"halted smoke-test: {json.loads(body)['halts']}")

try:
    # The halted agent's own call to an MCP server.
    status, headers, body = call(context("agents/smoke-test"), "POST", GATEWAY, INITIALIZE)
    assert status == 403 and headers.get("wake-halt") == HALT, (status, dict(headers), body)
    assert HALT in (headers.get("wake-halts") or "").split(","), dict(headers)
    print(f"smoke-test's MCP call: {status}, wake-halt {headers.get('wake-halt')}")

    # Another agent's isn't denied. Its MCP server may not be there; that's not a halt.
    status, headers, body = call(context("agents/research"), "POST", GATEWAY, INITIALIZE)
    assert headers.get("wake-halt") is None, (status, dict(headers), body)
    print(f"research's MCP call: {status}, not halted")

    # No SVID, no call. Under TLS 1.3 the refusal can come after the client's side of the
    # handshake, as an alert or a closed connection on the first read.
    try:
        status, headers, body = call(context(), "POST", GATEWAY, INITIALIZE)
    except (OSError, http.client.HTTPException) as e:
        print(f"a caller with no SVID: refused ({type(e).__name__})")
    else:
        raise AssertionError(f"a caller with no SVID got an answer: {status} {dict(headers)}")
finally:
    status, _, body = call(brake, "DELETE", f"{CONTROL}/v1/halts/{HALT}")
    assert status == 200, (status, body)

status, headers, body = call(context("agents/smoke-test"), "POST", GATEWAY, INITIALIZE)
assert headers.get("wake-halt") is None, (status, dict(headers), body)
print("lifted: smoke-test's MCP call isn't halted")
