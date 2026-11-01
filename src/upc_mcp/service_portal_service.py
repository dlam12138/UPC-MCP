"""Read-only e站通 service: catalog/guide and self todo/application progress.

Public catalog/guide reads are anonymous. Personal reads use only the unified
CAS coordinator and this adapter's independent DPAPI session/store/lock. No
write endpoint, no form/attachment/third-party page, and no ID walking exists.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .auto_auth.errors import AutoAuthError
from .auto_auth.locks import FileLock
from .service_portal_protocol import (
    MAX_PAGE,
    MAX_PAGE_SIZE,
    ServicePortalClient,
    ServicePortalError,
    now,
)
from .service_portal_store import (
    ServicePortalSession,
    ServicePortalStore,
    jar_from_cookies,
)

if TYPE_CHECKING:
    from .auto_auth.coordinator import AutoAuthCoordinator
    from .auto_auth.credentials import Binding

ACTIVE_STATES = {"stored", "unverified"}
NETWORK_CONCURRENCY = 4


class ServicePortalService:
    def __init__(
        self,
        store: ServicePortalStore,
        *,
        client_factory: Callable[[Any], ServicePortalClient] | None = None,
        auto_auth: AutoAuthCoordinator | None = None,
        auth_lock_timeout: float = 45.0,
        sso_only: bool = False,
    ):
        self.store = store
        self.auto_auth = auto_auth
        self.client_factory = client_factory or (
            lambda cookies: ServicePortalClient(cookies=cookies)
        )
        self.auth_lock_timeout = auth_lock_timeout
        self.sso_only = sso_only
        self._auth_lock = asyncio.Lock()
        self._network_gate = asyncio.Semaphore(NETWORK_CONCURRENCY)
        self._departments: dict[int, str] = {}
        self._department_lock = threading.Lock()

    @staticmethod
    def _base(status: str, *, anonymous: bool = False, query: dict | None = None) -> dict:
        return {
            "status": status,
            "source_system": "service_portal",
            "session_ref": "anonymous" if anonymous else "self",
            "fetched_at": now(),
            "query": query or {},
            "warnings": [],
        }

    @staticmethod
    def _map_auto(error: AutoAuthError) -> ServicePortalError:
        code = error.code
        details: dict[str, Any] = {}
        if code in {"credential_missing", "credential_reconfigure_required"}:
            return ServicePortalError(
                "auth_required",
                "统一认证尚未配置或密码已被拒绝；请在本机完成凭据配置。",
                action="credential_configure",
            )
        if code in {"user_action_required", "cas_captcha_required", "cas_second_factor_required"}:
            kind = error.details.get("kind")
            if isinstance(kind, str) and kind:
                details["kind"] = kind
            return ServicePortalError(
                "user_action_required",
                "统一认证需要验证码或二次验证；请完成官方交互后重试。",
                action="auto_auth_unlock",
                details=details,
            )
        if code == "service_second_factor_required":
            return ServicePortalError(
                "user_action_required",
                "e站通要求二次验证；请完成官方交互后重试。",
                action="service_portal_unlock",
                details={"kind": "second_factor"},
            )
        if code == "auth_required":
            return ServicePortalError(
                "auth_required",
                "e站通需要本人登录；当前未建立可用会话。",
                action="service_portal_auto_auth",
            )
        if code in {"recovery_backoff", "auth_in_progress"}:
            retry = error.details.get("retry_after_seconds")
            if isinstance(retry, (int, float)) and retry >= 0:
                details["retry_after_seconds"] = round(float(retry), 1)
            return ServicePortalError(
                "auth_in_progress",
                "另一次 e站通自动认证正在进行或短暂退避；本次未重复认证。",
                action="retry_later",
                details=details,
            )
        if code == "credential_changed":
            return ServicePortalError(
                "credential_changed",
                "统一凭据已更换或清除；本次 e站通认证结果未采用。",
                action="service_portal_clear",
            )
        if code == "identity_mismatch":
            return ServicePortalError(
                "identity_mismatch",
                "现有 e站通业务会话与统一凭据不一致；请先清除该会话。",
                action="service_portal_clear",
            )
        if code in {"service_timeout", "cas_timeout"}:
            return ServicePortalError("service_timeout", "e站通或 CAS 请求超时。")
        if code in {"service_network_error", "cas_network_error", "cas_tls_error"}:
            return ServicePortalError("service_network_error", "e站通或 CAS 网络请求失败。")
        if code in {"service_upstream_error", "cas_upstream_error"}:
            return ServicePortalError("service_upstream_error", "e站通或 CAS 服务暂时不可用。")
        if code in {
            "service_protocol_error",
            "service_unexpected_redirect",
            "service_redirect_limit",
            "service_token_missing",
            "service_response_too_large",
            "cas_unexpected_redirect",
            "cas_ticket_missing",
        }:
            return ServicePortalError(
                "schema_changed", "e站通认证响应结构不符合已验证契约。"
            )
        if code in {"service_auth_failed", "cas_access_denied", "cas_checkpoint_corrupt"}:
            return ServicePortalError(
                "auth_required",
                "e站通未能建立新会话；请稍后重试或检查统一认证状态。",
                action="service_portal_auto_auth",
            )
        return ServicePortalError("local_error", "e站通自动认证本地失败。")

    def _binding(self) -> Binding:
        if self.auto_auth is None:
            raise ServicePortalError(
                "auth_required",
                "统一认证未接入；本人 e站通查询不可用。",
                action="credential_configure",
            )
        try:
            return self.auto_auth.binding()
        except AutoAuthError as exc:
            raise self._map_auto(exc) from None

    @staticmethod
    def _account_mismatch(session: ServicePortalSession, binding: Binding) -> bool:
        return bool(
            session.account_hash
            and binding.account_hash
            and session.account_hash != binding.account_hash
        )

    @classmethod
    def _usable(cls, session: ServicePortalSession | None, binding: Binding) -> bool:
        if session is None or session.state not in ACTIVE_STATES:
            return False
        if not binding.configured:
            return False
        if cls._account_mismatch(session, binding):
            raise ServicePortalError(
                "identity_mismatch",
                "现有 e站通业务会话属于另一统一账户；请先清除该会话。",
                action="service_portal_clear",
            )
        return (
            session.account_hash == binding.account_hash
            and session.credential_generation == binding.generation
        )

    def _client(self, session: ServicePortalSession | None) -> ServicePortalClient:
        cookies = jar_from_cookies(session.cookies) if session is not None else None
        return self.client_factory(cookies)

    async def _snapshot(self) -> tuple[ServicePortalSession | None, int]:
        return await asyncio.to_thread(self.store.snapshot)

    async def _ensure_session(self) -> tuple[ServicePortalSession, int, Binding, bool]:
        binding = await asyncio.to_thread(self._binding)
        if not binding.configured:
            raise ServicePortalError(
                "auth_required",
                "本机尚未配置统一凭据。",
                action="credential_configure",
            )
        session, generation = await self._snapshot()
        if self._usable(session, binding):
            return session, generation, binding, False  # type: ignore[return-value]
        recovered, generation, binding = await self._recover(
            session, generation, binding
        )
        return recovered, generation, binding, True

    async def _recover(
        self,
        session: ServicePortalSession | None,
        generation: int,
        binding: Binding,
    ) -> tuple[ServicePortalSession, int, Binding]:
        async with self._auth_lock:
            current, current_generation = await self._snapshot()
            if self._usable(current, binding):
                return current, current_generation, binding  # type: ignore[return-value]
            if current is not None and self._account_mismatch(current, binding):
                raise ServicePortalError(
                    "identity_mismatch",
                    "现有 e站通业务会话属于另一统一账户；请先清除该会话。",
                    action="service_portal_clear",
                )
            if current_generation != generation:
                raise ServicePortalError(
                    "auth_required",
                    "e站通会话已变化；本次旧认证恢复已丢弃。",
                )
            return await asyncio.to_thread(
                self._recover_sync, current, current_generation, binding
            )

    def _check_current_binding_locked(self, binding: Binding) -> None:
        assert self.auto_auth is not None
        current = self.auto_auth.credentials.snapshot_locked()
        if (
            current.configured != binding.configured
            or current.generation != binding.generation
            or current.account_hash != binding.account_hash
        ):
            raise ServicePortalError(
                "credential_changed", "统一凭据已清除或更换；本次旧结果已丢弃。"
            )

    def _finish_sync(self, session: ServicePortalSession, binding: Binding) -> None:
        assert self.auto_auth is not None
        try:
            with self.auto_auth.credentials.guard():
                self._check_current_binding_locked(binding)
                self.store.finish(session.revision)
        except AutoAuthError as exc:
            raise self._map_auto(exc) from None

    def _recover_sync(
        self,
        session: ServicePortalSession | None,
        generation: int,
        binding: Binding,
    ) -> tuple[ServicePortalSession, int, Binding]:
        if self.auto_auth is None:
            raise ServicePortalError(
                "auth_required",
                "统一认证未接入；本人 e站通查询不可用。",
                action="credential_configure",
            )
        lock_path = self.store.directory / "service-portal-auth.lock"
        try:
            with FileLock(lock_path, timeout=self.auth_lock_timeout).held():
                with self.auto_auth.credentials.guard():
                    self._check_current_binding_locked(binding)
                current, current_generation = self.store.snapshot()
                if self._usable(current, binding):
                    return current, current_generation, binding  # type: ignore[return-value]
                if current is not None and self._account_mismatch(current, binding):
                    raise ServicePortalError(
                        "identity_mismatch",
                        "现有 e站通业务会话属于另一统一账户；请先清除该会话。",
                        action="service_portal_clear",
                    )
                if current_generation != generation:
                    raise ServicePortalError(
                        "auth_required",
                        "e站通会话已变化；本次旧认证恢复已丢弃。",
                    )
                try:
                    recovered = self.auto_auth.recover_sync(
                        "service_portal", allow_password=not self.sso_only
                    )
                except AutoAuthError as exc:
                    raise self._map_auto(exc) from None
                if not isinstance(recovered, ServicePortalSession):
                    raise ServicePortalError(
                        "schema_changed", "e站通自动认证返回了非预期会话对象。"
                    )
                if (
                    recovered.account_hash != binding.account_hash
                    or recovered.credential_generation != binding.generation
                ):
                    raise ServicePortalError(
                        "credential_changed",
                        "e站通恢复结果与当前统一凭据绑定不一致。",
                        action="service_portal_clear",
                    )
                try:
                    with self.auto_auth.credentials.guard():
                        self._check_current_binding_locked(binding)
                        self.store.replace(
                            recovered,
                            expected_revision=current.revision if current else None,
                            expected_generation=current_generation,
                        )
                except ServicePortalError as exc:
                    if exc.code == "auth_required":
                        raise ServicePortalError(
                            "auth_required",
                            "e站通会话在恢复期间被清除或更换；本次结果已丢弃。",
                        ) from None
                    raise
                # A clear that landed just after the conditional write must not
                # let this in-flight read use the already-deleted session.
                stored, _stored_generation = self.store.snapshot()
                if stored is None or stored.revision != recovered.revision:
                    raise ServicePortalError(
                        "auth_required",
                        "e站通会话在恢复期间被清除；本次结果已丢弃。",
                    )
                return recovered, current_generation + 1, binding
        except AutoAuthError as exc:
            raise self._map_auto(exc) from None

    async def _with_session(
        self, operation: Callable[[ServicePortalClient, Binding], dict]
    ) -> tuple[dict, ServicePortalSession]:
        session, generation, binding, recovered = await self._ensure_session()
        for attempt in range(2):
            client = self._client(session)
            try:
                async with self._network_gate:
                    payload = await asyncio.to_thread(operation, client, binding)
            except ServicePortalError as exc:
                if (
                    exc.code in {"auth_required", "auth_expired"}
                    and attempt == 0
                    and not recovered
                ):
                    try:
                        await asyncio.to_thread(self.store.mark_expired, session.revision)
                    except ServicePortalError:
                        pass
                    session, generation, binding = await self._recover(
                        session, generation, binding
                    )
                    recovered = True
                    continue
                raise
            finally:
                client.close()
            # Local clear/rebind is not a school authentication failure. Do
            # not put this check inside the recovery handler above.
            await asyncio.to_thread(self._finish_sync, session, binding)
            return payload, session
        raise ServicePortalError("auth_required", "e站通会话仍不可用。")

    async def search(
        self, *, keyword: str | None, page: int = 1, page_size: int = 20
    ) -> dict:
        if not 1 <= page <= MAX_PAGE:
            raise ServicePortalError("invalid_input", "page 超出允许范围。")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ServicePortalError("invalid_input", "page_size 超出允许范围。")
        if keyword is not None and not isinstance(keyword, str):
            raise ServicePortalError("invalid_input", "keyword 必须是字符串。")
        keyword = keyword.strip() if isinstance(keyword, str) else None
        if keyword == "":
            keyword = None
        if keyword is not None and len(keyword) > 40:
            raise ServicePortalError("invalid_input", "keyword 超过 40 个字符。")

        async with self._network_gate:
            result = await asyncio.to_thread(
                self._search_sync, keyword, page, page_size
            )
        payload = self._base(
            "ok",
            anonymous=True,
            query={"keyword": keyword, "page": page, "page_size": page_size},
        )
        payload.update(result)
        with self._department_lock:
            for item in result["items"]:
                if item.get("department"):
                    self._departments[item["id"]] = item["department"]
        payload["coverage"] = {"anonymous": True, "page": page, "page_size": page_size}
        return payload

    def _search_sync(self, keyword: str | None, page: int, page_size: int) -> dict:
        client = self._client(None)
        try:
            return client.search(keyword=keyword, page=page, page_size=page_size)
        finally:
            client.close()

    async def guide(self, service_id: int) -> dict:
        with self._department_lock:
            department = self._departments.get(service_id)
        async with self._network_gate:
            result = await asyncio.to_thread(
                self._guide_sync, service_id, department
            )
        payload = self._base("ok", anonymous=True, query={"service_id": service_id})
        payload.update(result)
        if department is None:
            payload["warnings"].append("department_not_cached")
        payload["coverage"] = {"anonymous": True, "guide_source": "official_detail"}
        return payload

    def _guide_sync(self, service_id: int, department: str | None) -> dict:
        client = self._client(None)
        try:
            return client.guide(service_id, department=department)
        finally:
            client.close()

    async def my_todos(self, *, page: int = 1) -> dict:
        if page != 1:
            raise ServicePortalError(
                "invalid_input", "当前仅验证了待办第 1 页；不提供其它页。"
            )
        result, _session = await self._with_session(
            lambda client, current: client.todos(current.account_hash)
        )
        payload = self._base(
            "ok",
            query={"page": 1, "status": 1},
        )
        payload.update(result)
        payload["coverage"] = {
            "page": 1,
            "status_filter": 1,
            "all_statuses": False,
            "full_history": False,
        }
        return payload

    async def my_applications(self, *, page: int = 1) -> dict:
        if page != 1:
            raise ServicePortalError(
                "invalid_input", "当前仅验证了申请第 1 页；不提供其它页。"
            )
        result, _session = await self._with_session(
            lambda client, current: client.applications(current.account_hash)
        )
        payload = self._base(
            "ok",
            query={"page": 1, "status": 1},
        )
        payload.update(result)
        payload["coverage"] = {
            "page": 1,
            "status_filter": 1,
            "all_statuses": False,
            "full_history": False,
            "third_party_records_marked": True,
        }
        return payload

    async def check(self) -> dict:
        result, session = await self._with_session(lambda client, _binding: client.verify())
        payload = self._base("ok")
        payload["verification"] = "message_count"
        payload.update(result)
        payload["session"] = session.public()
        return payload

    async def status(self) -> dict:
        binding: Binding | None
        try:
            binding = await asyncio.to_thread(self._binding)
        except ServicePortalError:
            binding = None
        try:
            session = await asyncio.to_thread(self.store.load)
        except ServicePortalError as exc:
            session = None
            local_error = exc.code
        else:
            local_error = None
        if binding is None or not binding.configured:
            status = "auth_required"
        elif session is None:
            status = "auth_required"
        elif self._account_mismatch(session, binding):
            status = "identity_mismatch"
        elif session.credential_generation != binding.generation:
            status = "auth_required"
        elif session.state == "auth_expired":
            status = "auth_expired"
        else:
            status = "ok"
        payload = self._base(status)
        payload["identity_verified"] = False
        payload["credential"] = {
            "configured": bool(binding and binding.configured),
            "generation": binding.generation if binding else None,
            "account_ref": binding.account_ref() if binding else None,
        }
        if local_error:
            payload["warnings"].append(f"local_session_{local_error}")
        payload["session"] = session.public() if session else None
        return payload

    async def clear(self) -> dict:
        await asyncio.to_thread(self.store.clear)
        payload = self._base("ok")
        payload["cleared"] = True
        payload["generation"] = await asyncio.to_thread(self.store.generation)
        return payload
