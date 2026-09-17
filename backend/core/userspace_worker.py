"""Private child entry point: localhost relay to exactly one userspace iPhone."""
import asyncio
import hmac
import json
import logging
import os
import sys
import threading

MAX_LINE = 4096


async def relay(reader, writer, *, token, address, dial):
    remote_writer = None
    pumps = []
    try:
        line = await asyncio.wait_for(reader.readuntil(b"\n"), 5)
        if len(line) > MAX_LINE:
            return
        request = json.loads(line)
        if not isinstance(request, dict):
            return
        supplied = request.get("token")
        port = request.get("port")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), token.encode()):
            return
        if type(port) is not int or not 1 <= port <= 65535:
            return
        remote_reader, remote_writer = await asyncio.wait_for(dial(address, port), 10)
        writer.write(b'{"status":"ok"}\n')
        await writer.drain()

        async def pump(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()
            if destination.can_write_eof():
                destination.write_eof()
                await destination.drain()

        pumps = [asyncio.create_task(pump(reader, remote_writer)),
                 asyncio.create_task(pump(remote_reader, writer))]
        await asyncio.gather(*pumps)
    except (OSError, ValueError, TypeError, asyncio.TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError):
        pass
    finally:
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        for stream in (remote_writer, writer):
            if stream:
                stream.close()
                try:
                    await asyncio.wait_for(stream.wait_closed(), 2)
                except (OSError, asyncio.TimeoutError):
                    pass


async def run(config):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    parent_fd = sys.stdin.fileno()

    def parent_closed():
        # The initial config line was already consumed by main. No additional
        # protocol input is expected. A raw read avoids holding BufferedReader's
        # lock in this daemon when the tunnel ends before the parent closes stdin.
        try:
            while os.read(parent_fd, 4096):
                pass
        except OSError:
            pass
        try:
            loop.call_soon_threadsafe(stop.set)
        except RuntimeError:
            pass

    threading.Thread(target=parent_closed, daemon=True).start()
    await _run_until_parent_closed(config, stop)


async def _run_until_parent_closed(config, stop):
    """Observe parent death during pairing as well as after tunnel readiness."""
    session = asyncio.create_task(_session(config, stop))
    parent = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((session, parent), return_when=asyncio.FIRST_COMPLETED)
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        if not session.done():
            session.cancel()
            _, pending = await asyncio.wait((session,), timeout=5)
            if pending:
                # This is a disposable child with no surviving owner. A library
                # swallowing cancellation must not leave an orphan tunnel.
                logging.getLogger(__name__).error("Parent closed; child cleanup timed out")
                os._exit(1)
        result = (await asyncio.gather(session, return_exceptions=True))[0]
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


async def _session(config, stop):
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
    tunnel_service.USE_USERSPACE_TUNNEL = True
    service = None
    clients = set()
    try:
        service = await tunnel_service.create_core_device_tunnel_service_using_remotepairing(
            config["udid"], config["ip"], config["port"])
        async with service.start_tcp_tunnel() as tunnel:
            tun = tunnel.client.tun
            tun.set_peer(tunnel.address)
            async with UserspaceDialPlane(tun, tunnel.address) as plane:

                def connected(reader, writer):
                    if len(clients) >= 32:
                        writer.close()
                        return
                    task = asyncio.create_task(relay(reader, writer, token=config["token"],
                                                      address=tunnel.address, dial=plane.dial))
                    clients.add(task)
                    def finished(completed):
                        clients.discard(completed)
                        if not completed.cancelled() and completed.exception():
                            logging.getLogger(__name__).warning(
                                "Device relay failed: %s", completed.exception())
                    task.add_done_callback(finished)

                async with await asyncio.start_server(connected, "127.0.0.1", 0, limit=MAX_LINE) as server:
                    print(json.dumps({"status": "ready", "relay_port": server.sockets[0].getsockname()[1],
                                      "rsd_address": tunnel.address, "rsd_port": tunnel.port,
                                      "interface": tunnel.interface, "protocol": str(tunnel.protocol)}), flush=True)
                    waits = [asyncio.create_task(stop.wait()),
                             asyncio.create_task(tunnel.client.wait_closed())]
                    try:
                        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                        if not stop.is_set() and waits[1].done():
                            waits[1].result()
                            logging.getLogger(__name__).warning(
                                "Device tunnel transport closed for %s at %s:%s",
                                config["udid"], config["ip"], config["port"])
                    finally:
                        for task in waits:
                            task.cancel()
                        await asyncio.gather(*waits, return_exceptions=True)
                        owned = list(clients)
                        for task in owned:
                            task.cancel()
                        await asyncio.gather(*owned, return_exceptions=True)
    finally:
        if service:
            await service.close()


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        line = sys.stdin.buffer.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE:
            raise ValueError("oversized child configuration")
        config = json.loads(line)
        if not isinstance(config.get("token"), str) or len(config["token"]) != 64:
            raise ValueError("invalid child authentication")
        asyncio.run(run(config))
    except Exception as exc:
        logging.getLogger(__name__).exception("Userspace child failed")
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
