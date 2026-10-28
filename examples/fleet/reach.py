"""Checks what one fleet container can reach. run.sh runs it in each agent and the tools
server, to show that the only way to the rest of the fleet is through agentgateway.

    python reach.py NAME HOST:PORT... -- HOST:PORT...

Each address before -- must connect. None after it may: a name mustn't resolve, and an
IP address mustn't connect.
"""

import ipaddress
import socket
import sys
from concurrent.futures import ThreadPoolExecutor


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def probe(address: str) -> str:
    """What happens on a call to the address: it "connects", "doesn't resolve" or "doesn't connect"."""
    host, port = address.rsplit(":", 1)
    try:
        socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "doesn't resolve"
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
        return "connects"
    except OSError:
        return "doesn't connect"


def main(name: str, args: list[str]) -> int:
    split = args.index("--") if "--" in args else len(args)
    must, must_not = args[:split], args[split + 1 :]
    # Together, since an address another network drops only fails when the timeout runs out.
    with ThreadPoolExecutor(len(must) + len(must_not) or 1) as pool:
        seen = dict(zip(must + must_not, pool.map(probe, must + must_not)))
    failures = [f"{a} {seen[a]}, and it must connect" for a in must if seen[a] != "connects"]
    for a in must_not:
        if seen[a] == "connects":
            failures.append(f"{a} connects around the gateway")
        elif seen[a] == "doesn't connect" and not is_ip(a.rsplit(":", 1)[0]):
            failures.append(f"{a} resolves, though it doesn't connect")
    if failures:
        print("\n".join(f"{name}: {f}" for f in failures), file=sys.stderr)
        return 1
    print(f"{name} reaches {', '.join(must)}, and none of {len(must_not)} other addresses")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
