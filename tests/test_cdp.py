import asyncio
import json
from urllib.parse import urlencode

import pytest
from conftest import COOKIE
from websockets.asyncio.server import serve

from upc_mcp.cdp import capture, local_endpoint
from upc_mcp.errors import UPCError
from upc_mcp.jhzf import BILL_ENDPOINT, BILL_PAGE


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://localhost:9222",
        "http://192.168.1.2:9222",
        "https://127.0.0.1:9222",
        "http://127.0.0.1",
        "ws://user@127.0.0.1:9222",
        "http://127.0.0.1:9222/x",
    ],
)
def test_cdp_does_not_connect_to_remote_or_credential_urls(endpoint):
    with pytest.raises(UPCError):
        local_endpoint(endpoint)


def test_passive_capture_handles_early_cookie_event_and_ignores_unrelated_session(form, bill):
    async def scenario():
        methods, attached, detached = [], [], []

        async def backend(websocket):
            async for raw in websocket:
                request = json.loads(raw)
                method, params = request["method"], request["params"]
                methods.append(method)
                response = {}
                if method == "Target.getTargets":
                    response = {
                        "targetInfos": [
                            {"targetId": "own-page", "type": "page", "url": BILL_PAGE},
                            {
                                "targetId": "other-page",
                                "type": "page",
                                "url": "https://other.invalid/",
                            },
                        ]
                    }
                elif method == "Target.attachToTarget":
                    attached.append(params["targetId"])
                    response = {"sessionId": "own-session"}
                elif method == "Target.detachFromTarget":
                    detached.append(params["sessionId"])
                elif method == "Network.getResponseBody":
                    response = {"body": json.dumps(bill), "base64Encoded": False}
                await websocket.send(json.dumps({"id": request["id"], "result": response}))
                if method == "Network.enable":
                    events = [
                        {
                            "sessionId": "unrelated-session",
                            "method": "Network.requestWillBeSent",
                            "params": {
                                "requestId": "unrelated",
                                "request": {
                                    "method": "POST",
                                    "url": BILL_ENDPOINT,
                                    "postData": "unrelated-private-data",
                                },
                            },
                        },
                        {
                            "method": "Network.requestWillBeSentExtraInfo",
                            "params": {"requestId": "own-request", "headers": {"Cookie": COOKIE}},
                        },
                        {
                            "method": "Network.requestWillBeSent",
                            "params": {
                                "requestId": "own-request",
                                "request": {
                                    "method": "POST",
                                    "url": BILL_ENDPOINT,
                                    "postData": urlencode(form),
                                    "headers": {"Accept": "*/*"},
                                },
                            },
                        },
                        {
                            "method": "Network.responseReceived",
                            "params": {"requestId": "own-request", "response": {"status": 200}},
                        },
                        {
                            "method": "Network.loadingFinished",
                            "params": {"requestId": "own-request"},
                        },
                    ]
                    for event in events:
                        event.setdefault("sessionId", "own-session")
                        await websocket.send(json.dumps(event))

        async with serve(backend, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            session = await capture(f"ws://127.0.0.1:{port}", 2)
        assert session.cookie.get_secret_value() == COOKIE
        assert attached == ["own-page"]
        assert detached == ["own-session"]
        assert not set(methods) & {
            "Page.reload",
            "Page.navigate",
            "Runtime.evaluate",
            "Network.getAllCookies",
            "Storage.getCookies",
        }

    asyncio.run(scenario())
