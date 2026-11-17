"""Persistent direct SIP client and authenticated Hermes control API."""

import argparse
import asyncio
import hmac
import logging
import os
import shutil
import signal
from pathlib import Path

import aiohttp
from aiohttp import web

from .config import Config
from .conversation import Conversations
from .sip import SipClient
from .store import ID, Store, validate_call
from .voice import Voice

log = logging.getLogger(__name__)


class Dialer:
    def __init__(self, config: Config, store: Store, sip: SipClient):
        self.config, self.store, self.sip = config, store, sip

    async def dial(self, data: dict) -> dict:
        call = validate_call(data, self.config.allowed_extensions)
        # No await between the ledger check and INVITE submission: serialized on SIP owner thread.
        if self.store.get(call["id"]):
            result, _ = self.store.add(call)
            return result
        if not self.sip.registered:
            raise web.HTTPServiceUnavailable(
                text='{"error":"SIP account is not registered"}', content_type="application/json"
            )
        if self.sip.busy:
            raise web.HTTPConflict(
                text='{"error":"telephone is busy"}', content_type="application/json"
            )
        result, created = self.store.add(call)
        assert created
        try:
            self.sip.dial(call["id"], call["extension"], call["message"])
        except Exception as exc:
            self.store.transition(
                call["id"],
                "dialing",
                "unknown",
                "INVITE submission uncertain; inspect SIP server before redialing",
            )
            log.warning("SIP dial uncertain (%s)", type(exc).__name__)
        return self.store.get(call["id"]) or result


def application(config: Config, store: Store, dialer: Dialer) -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):
        value = request.headers.get("Authorization", "")
        if not hmac.compare_digest(value.encode(), ("Bearer " + config.api_token).encode()):
            raise web.HTTPUnauthorized()
        try:
            return await handler(request)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def dial(request: web.Request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        result = await dialer.dial(data)
        return web.json_response(result, status=202 if result["state"] == "dialing" else 200)

    async def calls(request: web.Request):
        return web.json_response({"calls": store.list()})

    async def health(request: web.Request):
        sip = dialer.sip
        return web.json_response(
            {
                "sip_registered": sip.registered,
                "sip_status": sip.registration_status,
                "busy": sip.busy,
            },
            status=200 if sip.registered else 503,
        )

    app = web.Application(middlewares=[auth], client_max_size=16384)
    app.add_routes([web.post("/calls", dial), web.get("/calls", calls), web.get("/health", health)])
    return app


async def serve(config: Config) -> None:
    import fcntl

    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (config.data_dir / "service.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        media = config.data_dir / "media"
        media.mkdir(mode=0o700, exist_ok=True)
        for item in media.iterdir():
            if item.is_dir() and ID.fullmatch(item.name):
                shutil.rmtree(item)
        store = Store(config.data_dir / "calls.sqlite")
        store.recover()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
            conversations = Conversations(config, store, Voice(config, http))
            sip = SipClient(config, conversations.accept)
            conversations.sip = sip
            runner = web.AppRunner(
                application(config, store, Dialer(config, store, sip)),
                access_log=None,
                shutdown_timeout=5,
            )
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            tasks = []
            try:
                sip.start()
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", config.api_port).start()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, stopped.set)
                tasks = [asyncio.create_task(sip.poll()), asyncio.create_task(stopped.wait())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task  # A failed SIP event loop must stop/restart the service.
            finally:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
                await runner.cleanup()
                await conversations.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await sip.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/hermes-call/config.json"
    )
    parser.add_argument(
        "--check", action="store_true", help="validate config and native SIP binding"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.load(args.config)
    if args.check:
        import importlib

        importlib.import_module("pjsua2")
        print("Configuration and PJSUA2 import valid (network registration not checked)")
        return
    os.umask(0o077)
    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
