"""A stand-in for the research agent behind deploy's A2A route, for smoke_test.sh: it
answers any message with a stream that sends a status update every half second for a
minute, so the test can halt a call while it's under way and see it cut.

Standard library only. Listens on port 9999, where the route sends research's calls.
"""

import http.server
import json
import time


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for i in range(120):
            update = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"kind": "status-update", "taskId": "t1", "contextId": "c1",
                           "status": {"state": "working", "message": f"step {i}"}, "final": False},
            }
            try:
                self.wfile.write(f"data: {json.dumps(update)}\n\n".encode())
                self.wfile.flush()
            except OSError:
                return  # the gateway cut the call
            time.sleep(0.5)

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("0.0.0.0", 9999), Handler).serve_forever()
