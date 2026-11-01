"""Passive, local-only handoff from the user's normal successful bill request."""

import asyncio
import json
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

from .auth import candidate
from .errors import UPCError
from .jhzf import BILL_ENDPOINT, BILL_PAGE


def local_endpoint(value: str, schemes=("http", "ws")) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in schemes
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.path not in {"", "/"})
        ):
            raise ValueError
        return value
    except ValueError:
        raise UPCError("invalid_input", "调试端点必须是带显式端口的本机 IP，不能含凭据。") from None


async def resolve_ws(endpoint: str) -> str:
    local_endpoint(endpoint)
    if endpoint.startswith("ws:"):
        return endpoint
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
            response = await client.get(endpoint.rstrip("/") + "/json/version")
            if response.status_code != 200 or len(response.content) > 65536:
                raise ValueError
            return local_endpoint(response.json()["webSocketDebuggerUrl"], ("ws",))
    except (httpx.HTTPError, ValueError, KeyError):
        raise UPCError("auth_required", "无法读取本机真实浏览器调试入口。") from None


async def capture(endpoint: str, window_seconds: int = 180):
    """No refresh, navigation, JavaScript evaluation or cookie/profile enumeration."""
    endpoint = await resolve_ws(endpoint)
    try:
        async with connect(
            endpoint, open_timeout=5, close_timeout=2, max_size=4 * 1024 * 1024, ping_interval=None
        ) as websocket:
            return await _capture(websocket, window_seconds)
    except UPCError:
        raise
    except Exception:
        raise UPCError("auth_required", "调试连接已断开，请检查本机授权承接通道。") from None


async def _capture(websocket, window_seconds):
    pending, scopes, requests, extras = {}, {}, {}, {}
    queue = asyncio.Queue(maxsize=2048)
    command_counter = 1 << 29

    async def reader():
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if message.get("id") in pending:
                    future = pending[message["id"]]
                    if not future.done():
                        future.set_result(message)
                elif "method" in message:
                    queue.put_nowait(message)
        finally:
            queue.put_nowait({"closed": True})

    async def call(method, params=None, session_id=None):
        nonlocal command_counter
        command_counter += 1
        identifier = command_counter
        future = asyncio.get_running_loop().create_future()
        pending[identifier] = future
        try:
            await websocket.send(
                json.dumps(
                    {
                        "id": identifier,
                        "method": method,
                        "params": params or {},
                        **({"sessionId": session_id} if session_id else {}),
                    }
                )
            )
            message = await asyncio.wait_for(future, 5)
            if "error" in message:
                raise UPCError("unsupported", "调试通道不支持所需的真实 CDP 方法。")
            return message.get("result", {})
        finally:
            pending.pop(identifier, None)

    async def consider(target):
        identifier = target.get("targetId")
        if identifier in scopes.values():
            if target.get("url") != BILL_PAGE:
                for session_id, selected in list(scopes.items()):
                    if selected == identifier:
                        scopes.pop(session_id)
                        await call("Target.detachFromTarget", {"sessionId": session_id})
            return
        if target.get("type") == "page" and target.get("url") == BILL_PAGE:
            attached = await call(
                "Target.attachToTarget", {"targetId": identifier, "flatten": True}
            )
            session_id = attached.get("sessionId")
            if not isinstance(session_id, str):
                raise UPCError("unsupported", "内核未返回真实目标会话。")
            scopes[session_id] = identifier
            await call(
                "Network.enable",
                {"maxTotalBufferSize": 4194304, "maxResourceBufferSize": 1048576},
                session_id,
            )

    receiver = asyncio.create_task(reader())
    try:
        await call("Target.setDiscoverTargets", {"discover": True})
        for target in (await call("Target.getTargets")).get("targetInfos", []):
            await consider(target)
        deadline = asyncio.get_running_loop().time() + window_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise UPCError("auth_required", "等待正常账单成功查询超时，原有授权未被替换。")
            try:
                message = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError:
                raise UPCError(
                    "auth_required", "未观察到成功查询，请从官方入口重新进入账单。"
                ) from None
            if message.get("closed"):
                raise UPCError("auth_required", "本机调试通道已断开。")
            method, params = message.get("method"), message.get("params", {})
            if method in {"Target.targetCreated", "Target.targetInfoChanged"}:
                await consider(params.get("targetInfo", {}))
                continue
            session_id = message.get("sessionId")
            if session_id not in scopes:
                continue
            key = (session_id, params.get("requestId"))
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                if request.get("method") == "POST" and request.get("url") == BILL_ENDPOINT:
                    if len(requests) >= 20:
                        raise UPCError("auth_required", "观察请求已达本地上限，请重新开始授权。")
                    requests[key] = {"request": request}
            if method == "Network.requestWillBeSentExtraInfo":
                # Only selected target/session metadata; never write raw headers to disk/logs.
                if len(extras) < 100:
                    extras[key] = params.get("headers", {})
            item = requests.get(key)
            if not item:
                continue
            if method == "Network.responseReceived":
                item["status"] = params.get("response", {}).get("status")
            if method == "Network.loadingFinished" and item.get("status") == 200:
                body = await call("Network.getResponseBody", {"requestId": key[1]}, session_id)
                if body.get("base64Encoded") or not isinstance(body.get("body"), str):
                    continue
                item["body"] = body["body"]
            if "body" not in item or key not in extras:
                continue
            try:
                root = json.loads(item["body"])
                if str(root.get("retcode")) != "0":
                    continue
            except (ValueError, AttributeError):
                continue
            headers = {**item["request"].get("headers", {}), **extras[key]}
            cookie = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
            return candidate(item["request"].get("postData", ""), cookie, headers, item["body"])
    finally:
        if not receiver.done():
            for session_id in list(scopes):
                try:
                    await call("Target.detachFromTarget", {"sessionId": session_id})
                except (UPCError, TimeoutError):
                    pass
            try:
                await call("Target.setDiscoverTargets", {"discover": False})
            except (UPCError, TimeoutError):
                pass
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
