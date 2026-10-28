"""A stand-in for a design partner's identity provider, such as Okta: just enough of an OAuth
authorization server for the fleet's agents to hold tokens the brake can revoke. Under
/oauth2/v1, as Okta's org authorization server has them:

    POST /token                      an agent swaps its refresh token for an access token
    POST /revoke                     RFC 7009, for the client a token was issued to
    POST /introspect                 RFC 7662, for the client a token was issued to, and the
                                     tools server, for the access tokens it's the audience of
    POST /global-token-revocation    Okta's: signs a user out of everything, for an API token

and Okta's Users API, under /api/v1, for the same API token:

    GET /users/{user}/clients                    the clients the user holds tokens at
    GET /users/{user}/clients/{client}/tokens    the user's refresh tokens at one of them

At first start it issues each agent a refresh token, as if the agent had signed in as its own
user, through the agent's own public client. The agent's copy goes to the agent's volume. The
customer's copy, which the brake revokes and introspects as the agent's client, goes to the
brake's volume, with an API token for global token revocation and the Users API. The tools
server gets a client of its own, which may only introspect. No agent can read another's token
or anything the brake holds.

It's as strict as Okta's documentation says a server may be: a client sees only its own
tokens. Introspecting another client's token answers inactive, and revoking it does nothing,
with a 200, so a brake that asks as the wrong client would see a live token as revoked.

Revoking a refresh token also revokes the access tokens swapped for it (RFC 7009, section
2.1), and global token revocation revokes every token of the user. A confidential client's
secret goes in HTTP Basic, form-encoded first (RFC 6749, section 2.3.1); a public client
sends only its client_id, in the form.

    python -m fleet.standins.oauth
"""

import base64
import logging
import os
import threading
import time
import urllib.parse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from fleet.standins import digest, drop_privileges, hand_out, load, matches, new_secret, save

log = logging.getLogger("idp")

ISSUER = os.environ.get("STANDIN_ISSUER", "http://idp:8080")
AGENTS = os.environ.get("STANDIN_AGENTS", "support research billing notify").split()
TOOLS = "fleet-tools"
# The audience and scope of the access tokens: the fleet's tools server.
AUDIENCE = "fleet-tools"
SCOPE = "fleet.tools"
ACCESS_TTL = 3600
REFRESH_TTL = 90 * 24 * 3600
STATE_FILE = "oauth.json"


def user(agent: str) -> str:
    """The agent's user at the identity provider: what the brake's inventory names as its
    okta_user_id."""
    return f"fleet-{agent}"


class Provider:
    def __init__(self, state: dict, *, persist: bool = True) -> None:
        self.state = state
        self.lock = threading.Lock()
        self.persist = persist

    @classmethod
    def first_start(cls) -> "Provider":
        """Registers the clients and issues the agents' refresh tokens, handing each credential
        to its holder."""
        tools_secret, api_token = new_secret("tools_"), new_secret("okta_")
        p = cls(persist=False, state={
            "clients": {
                # A resource server: it may introspect the access tokens it's the audience of.
                TOOLS: {"secret": digest(tools_secret), "may": ["introspect"], "audience": AUDIENCE},
                **{agent: {"public": True} for agent in AGENTS},
            },
            "api_tokens": [digest(api_token)],
            "users": [user(agent) for agent in AGENTS],
            "tokens": {},
        })
        hand_out("brake", "okta-api-token", api_token)
        hand_out("tools", "oauth-client-secret", tools_secret)
        for agent in AGENTS:
            token = p.issue("refresh_token", agent, user(agent), REFRESH_TTL, scope=f"offline_access {SCOPE}")
            hand_out(agent, "refresh-token", token)
            hand_out("brake", f"tokens/{agent}", token)
        # Kept only once every credential is out, so a start cut short starts over.
        p.persist = True
        p.save()
        return p

    def save(self) -> None:
        if self.persist:
            save(STATE_FILE, self.state)

    def issue(self, kind: str, client: str, sub: str, ttl: int, *, scope: str, parent: str = "") -> str:
        token = new_secret("at_" if kind == "access_token" else "rt_")
        now = int(time.time())
        with self.lock:
            self.state["tokens"][digest(token)] = {
                "kind": kind, "client_id": client, "sub": sub, "scope": scope,
                "iat": now, "exp": now + ttl, "parent": parent, "revoked": False,
            }
            self.save()
        return token

    def find(self, token: str) -> tuple[str, dict] | tuple[None, None]:
        key = digest(token)
        t = self.state["tokens"].get(key)
        return (key, t) if t else (None, None)

    def active(self, t: dict | None) -> bool:
        return bool(t) and not t["revoked"] and t["exp"] > time.time()

    def revoke(self, keys: list[str]) -> int:
        """Revokes these tokens and the access tokens swapped for them. Returns how many
        tokens it revoked."""
        with self.lock:
            tokens = self.state["tokens"]
            doomed = set(keys) | {k for k, t in tokens.items() if t["parent"] in keys}
            count = 0
            for k in doomed:
                if k in tokens and not tokens[k]["revoked"]:
                    tokens[k]["revoked"] = True
                    count += 1
            self.save()
        return count

    def client(self, request: Request, form, may: str) -> str | None:
        """The client authenticating the request, if it may do this: a confidential one with
        its secret in Basic, or a public one, which may revoke and introspect its own tokens,
        with its client_id in the form."""
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            name = str(form.get("client_id", ""))
            return name if self.state["clients"].get(name, {}).get("public") else None
        try:
            name, _, secret = base64.b64decode(credentials, validate=True).decode().partition(":")
        except ValueError:
            return None
        name, secret = urllib.parse.unquote_plus(name), urllib.parse.unquote_plus(secret)
        c = self.state["clients"].get(name)
        if not c or c.get("public") or not matches(secret, c["secret"]) or may not in c["may"]:
            return None
        return name

    def sees(self, client: str, t: dict) -> bool:
        """Whether this client may know about the token: it was issued to the client, or it's
        an access token for the client's audience."""
        if t["client_id"] == client:
            return True
        audience = self.state["clients"].get(client, {}).get("audience")
        return t["kind"] == "access_token" and audience == AUDIENCE

    def api(self, request: Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and any(matches(token, d) for d in self.state["api_tokens"])


def invalid_client() -> JSONResponse:
    return JSONResponse({"error": "invalid_client"}, status_code=401, headers={"WWW-Authenticate": 'Basic realm="idp"'})


def invalid_token() -> JSONResponse:
    return JSONResponse({"errorCode": "E0000011", "errorSummary": "Invalid token provided"}, status_code=401)


def routes(p: Provider) -> list[Route]:
    async def token(request: Request) -> Response:
        form = await request.form()
        if form.get("grant_type") != "refresh_token":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        client_id = str(form.get("client_id", ""))
        if not p.state["clients"].get(client_id, {}).get("public"):
            return invalid_client()
        key, t = p.find(str(form.get("refresh_token", "")))
        if not p.active(t) or t["kind"] != "refresh_token" or t["client_id"] != client_id:
            log.warning("refused a refresh for %s: the refresh token isn't active", client_id)
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = p.issue("access_token", client_id, t["sub"], ACCESS_TTL, scope=SCOPE, parent=key)
        return JSONResponse({"token_type": "Bearer", "access_token": access, "expires_in": ACCESS_TTL, "scope": SCOPE})

    async def revoke(request: Request) -> Response:
        form = await request.form()
        client = p.client(request, form, "revoke")
        if not client:
            return invalid_client()
        if not form.get("token"):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        key, t = p.find(str(form["token"]))
        if t and t["client_id"] == client:
            count = p.revoke([key])
            log.info("%s revoked a %s of %s: %d tokens revoked", client, t["kind"], t["sub"], count)
        elif t:
            log.warning("%s tried to revoke a token issued to %s: ignored", client, t["client_id"])
        # 200 whether or not the token was valid, or the client's (RFC 7009, section 2.2).
        return Response(status_code=200)

    async def introspect(request: Request) -> Response:
        form = await request.form()
        client = p.client(request, form, "introspect")
        if not client:
            return invalid_client()
        _, t = p.find(str(form.get("token", "")))
        if not p.active(t) or not p.sees(client, t):
            return JSONResponse({"active": False})
        out = {"active": True, "iss": ISSUER, "client_id": t["client_id"], "sub": t["sub"], "username": t["sub"],
               "scope": t["scope"], "iat": t["iat"], "exp": t["exp"]}
        if t["kind"] == "access_token":
            out |= {"token_type": "Bearer", "aud": AUDIENCE}
        return JSONResponse(out)

    async def global_token_revocation(request: Request) -> Response:
        if not p.api(request):
            return invalid_token()
        try:
            sub_id = (await request.json())["sub_id"]
            fmt, sub = sub_id["format"], sub_id["id"]
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"errorCode": "E0000001", "errorSummary": "sub_id is required"}, status_code=400)
        if fmt != "opaque":
            return JSONResponse({"errorCode": "E0000001", "errorSummary": f"this stand-in takes only opaque sub_ids, not {fmt}"}, status_code=400)
        if sub not in p.state["users"]:
            return JSONResponse({"errorCode": "E0000007", "errorSummary": f"Not found: {sub}"}, status_code=404)
        count = p.revoke([k for k, t in p.state["tokens"].items() if t["sub"] == sub])
        log.info("signed %s out everywhere: %d tokens revoked", sub, count)
        return Response(status_code=204)

    def user_tokens(request: Request) -> list[dict] | JSONResponse:
        sub = request.path_params["user"]
        if sub not in p.state["users"]:
            return JSONResponse({"errorCode": "E0000007", "errorSummary": f"Not found: Resource not found: {sub} (User)"}, status_code=404)
        return [t for t in p.state["tokens"].values() if t["sub"] == sub]

    async def user_clients(request: Request) -> Response:
        if not p.api(request):
            return invalid_token()
        tokens = user_tokens(request)
        if isinstance(tokens, Response):
            return tokens
        clients = sorted({t["client_id"] for t in tokens})
        return JSONResponse([{"client_id": c, "client_name": c} for c in clients])

    async def user_client_tokens(request: Request) -> Response:
        if not p.api(request):
            return invalid_token()
        tokens = user_tokens(request)
        if isinstance(tokens, Response):
            return tokens
        client = request.path_params["client"]
        return JSONResponse([
            {"id": "oar" + k[:16], "status": "ACTIVE", "clientId": t["client_id"], "userId": t["sub"], "issuer": ISSUER,
             "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t["exp"]))}
            for k, t in p.state["tokens"].items()
            if t["sub"] == request.path_params["user"] and t["client_id"] == client and t["kind"] == "refresh_token" and p.active(t)
        ])

    return [
        Route("/oauth2/v1/token", token, methods=["POST"]),
        Route("/oauth2/v1/revoke", revoke, methods=["POST"]),
        Route("/oauth2/v1/introspect", introspect, methods=["POST"]),
        Route("/oauth2/v1/global-token-revocation", global_token_revocation, methods=["POST"]),
        Route("/api/v1/users/{user}/clients", user_clients, methods=["GET"]),
        Route("/api/v1/users/{user}/clients/{client}/tokens", user_client_tokens, methods=["GET"]),
    ]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    state = load(STATE_FILE)
    if state is None:
        p = Provider.first_start()
        log.info("issued refresh tokens to %s, and handed the brake its credentials", ", ".join(AGENTS))
    else:
        p = Provider(state)
    drop_privileges()
    uvicorn.run(Starlette(routes=routes(p)), host="0.0.0.0", port=int(os.environ.get("STANDIN_PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
