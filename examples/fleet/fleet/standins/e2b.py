"""A stand-in for the E2B API: just enough for a fleet agent to keep a sandbox and the brake to
freeze it, with the fields the brake reads (wake-platform's brake/sandbox):

    POST /sandboxes              create one, with metadata and lifecycle.autoResume
    GET  /v2/sandboxes           list them, by metadata ("k=v&k2=v2") and state
    GET  /sandboxes/{id}         read one: state, pauseMode, lifecycle.autoResume
    POST /sandboxes/{id}/pause   pause one with its memory, or with its filesystem only
                                 ({"memory": false}); 409 if it isn't running

It runs nothing: a sandbox here is only its record, and nothing sends it traffic, so nothing
auto-resumes one. There are two API keys on one team, as an E2B account would have: billing's,
which it keeps its sandbox with, and the brake's. Pauses are kept across restarts.

    python -m fleet.standins.e2b
"""

import logging
import os
import secrets
import threading
import time
import urllib.parse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from fleet.standins import digest, drop_privileges, hand_out, load, matches, new_secret, save

log = logging.getLogger("e2b")

# Who gets an API key: the agents that keep sandboxes, and the brake.
HOLDERS = os.environ.get("STANDIN_E2B_KEYS", "billing brake").split()
STATE_FILE = "e2b.json"


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"code": status, "message": message}, status_code=status)


class Account:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.lock = threading.Lock()

    @classmethod
    def first_start(cls) -> "Account":
        keys = {holder: new_secret("e2b_") for holder in HOLDERS}
        for holder, key in keys.items():
            hand_out(holder, "e2b-api-key", key)
        a = cls({"keys": {digest(k): holder for holder, k in keys.items()}, "sandboxes": {}})
        a.save()
        return a

    def save(self) -> None:
        save(STATE_FILE, self.state)

    def caller(self, request: Request) -> str | None:
        key = request.headers.get("x-api-key", "")
        for d, holder in self.state["keys"].items():
            if matches(key, d):
                return holder
        return None


def detail(s: dict) -> dict:
    out = {k: s[k] for k in ("sandboxID", "templateID", "clientID", "startedAt", "metadata", "state")}
    out["lifecycle"] = {"autoResume": s["autoResume"]}
    if s["state"] == "paused":
        out["pauseMode"] = s["pauseMode"]
    return out


def routes(a: Account) -> list[Route]:
    def authed(handler):
        async def checked(request: Request) -> Response:
            holder = a.caller(request)
            if holder is None:
                return error(401, "Invalid API key")
            return await handler(request, holder)

        return checked

    async def create(request: Request, holder: str) -> Response:
        try:
            body = await request.json()
            metadata = {str(k): str(v) for k, v in (body.get("metadata") or {}).items()}
            auto_resume = bool((body.get("lifecycle") or {}).get("autoResume", False))
        except (ValueError, AttributeError):
            return error(400, "invalid body")
        sid = "i" + secrets.token_hex(10)
        s = {"sandboxID": sid, "templateID": str(body.get("templateID") or "base"), "clientID": "standin",
             "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "metadata": metadata,
             "state": "running", "pauseMode": "", "autoResume": auto_resume}
        with a.lock:
            a.state["sandboxes"][sid] = s
            a.save()
        log.info("%s started sandbox %s with metadata %s, auto-resume %s", holder, sid, metadata, auto_resume)
        return JSONResponse(detail(s), status_code=201)

    async def listed(request: Request, holder: str) -> Response:
        want = dict(urllib.parse.parse_qsl(request.query_params.get("metadata", "")))
        states = request.query_params.getlist("state") or ["running", "paused"]
        found = [detail(s) for s in a.state["sandboxes"].values()
                 if s["state"] in states and all(s["metadata"].get(k) == v for k, v in want.items())]
        return JSONResponse(found)

    async def get(request: Request, holder: str) -> Response:
        s = a.state["sandboxes"].get(request.path_params["id"])
        if not s:
            return error(404, f"sandbox {request.path_params['id']} not found")
        return JSONResponse(detail(s))

    async def pause(request: Request, holder: str) -> Response:
        sid = request.path_params["id"]
        try:
            body = await request.json() if await request.body() else {}
            memory = bool(body.get("memory", True))
        except (ValueError, AttributeError):
            return error(400, "invalid body")
        with a.lock:
            s = a.state["sandboxes"].get(sid)
            if not s:
                return error(404, f"sandbox {sid} not found")
            if s["state"] != "running":
                return error(409, f"sandbox {sid} is {s['state']}")
            s["state"], s["pauseMode"] = "paused", "full" if memory else "filesystem"
            a.save()
        log.info("%s paused sandbox %s (%s)", holder, sid, s["pauseMode"])
        return Response(status_code=204)

    return [
        Route("/sandboxes", authed(create), methods=["POST"]),
        Route("/v2/sandboxes", authed(listed), methods=["GET"]),
        Route("/sandboxes/{id}", authed(get), methods=["GET"]),
        Route("/sandboxes/{id}/pause", authed(pause), methods=["POST"]),
    ]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    state = load(STATE_FILE)
    a = Account(state) if state is not None else Account.first_start()
    drop_privileges()
    uvicorn.run(Starlette(routes=routes(a)), host="0.0.0.0", port=int(os.environ.get("STANDIN_PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
