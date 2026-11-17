import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_call.service import Dialer, application
from hermes_call.store import Store


class FakeSIP:
    def __init__(self):
        self.registered = True
        self.registration_status = 200
        self.busy = False
        self.requests = []
        self.fail = False

    def dial(self, *args):
        self.requests.append(args)
        self.busy = True
        if self.fail:
            raise RuntimeError("native error")


def test_api_auth_and_idempotent_direct_dial(config, tmp_path):
    async def scenario():
        store = Store(tmp_path / "calls.sqlite")
        sip = FakeSIP()
        dialer = Dialer(config, store, sip)
        async with TestClient(TestServer(application(config, store, dialer))) as client:
            headers = {"Authorization": "Bearer " + config.api_token}
            assert (await client.get("/calls")).status == 401
            assert (await client.get("/health", headers=headers)).status == 200
            data = {"extension": "1001", "message": "hello", "request_id": "b" * 32}
            assert (await client.post("/calls", headers=headers, json=data)).status == 202
            assert (await client.post("/calls", headers=headers, json=data)).status == 202
            assert sip.requests == [("b" * 32, "1001", "hello")]
            assert (
                await client.post("/calls", headers=headers, json={**data, "request_id": "c" * 32})
            ).status == 409
            for invalid in ([], {**data, "extension": "9999"}, {**data, "delay_seconds": 10}):
                assert (await client.post("/calls", headers=headers, json=invalid)).status == 400
            assert (await client.post("/calls", headers=headers, data="{")).status == 400
            sip.registered = False
            assert (await client.get("/health", headers=headers)).status == 503
        with pytest.raises(web.HTTPServiceUnavailable):
            await dialer.dial({**data, "request_id": "d" * 32})
        assert len(sip.requests) == 1

    asyncio.run(scenario())


def test_uncertain_native_submission_not_retried(config, tmp_path):
    async def scenario():
        sip = FakeSIP()
        sip.fail = True
        dialer = Dialer(config, Store(tmp_path / "calls.sqlite"), sip)
        data = {"extension": "1001", "message": "hello", "request_id": "d" * 32}
        assert (await dialer.dial(data))["state"] == "unknown"
        assert (await dialer.dial(data))["state"] == "unknown"
        assert len(sip.requests) == 1

    asyncio.run(scenario())
