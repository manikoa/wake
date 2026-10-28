"""Passes the TCP connections it takes on one port to one address, and nothing else. It's how
billing reaches wake-proxy's memory rules (9003) with no way to the rest of wake-proxy, such
as the brake's control API (9002): see "Network" in README.md.

    python -m fleet.relay PORT HOST:PORT
"""

import asyncio
import logging
import signal
import sys

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
log = logging.getLogger("relay")


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(1 << 16):
        writer.write(data)
        await writer.drain()
    # The other way may still have more to send, as a reply does.
    if writer.can_write_eof():
        writer.write_eof()


async def serve(port: int, host: str, to: int) -> None:
    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(host, to)
        except OSError as e:
            log.warning("couldn't reach %s:%d: %s", host, to, e)
            writer.close()
            return
        ways = [asyncio.ensure_future(pipe(reader, up_writer)), asyncio.ensure_future(pipe(up_reader, writer))]
        try:
            await asyncio.wait(ways, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            # Done both ways, or broken one way: closing both ends the other.
            writer.close()
            up_writer.close()
            await asyncio.gather(*ways, return_exceptions=True)

    server = await asyncio.start_server(relay, "0.0.0.0", port)
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    log.info("relaying :%d to %s:%d", port, host, to)
    await stop.wait()
    server.close()


if __name__ == "__main__":
    host, to = sys.argv[2].rsplit(":", 1)
    asyncio.run(serve(int(sys.argv[1]), host, int(to)))
