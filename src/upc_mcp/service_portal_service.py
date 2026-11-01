"""Read-only e站通 service: catalog/guide and self todo/application progress.

Search uses the complete catalog visible to the saved account when one is
configured, then matches and paginates locally.  Without a usable account it
falls back to the public catalog and labels that scope explicitly.  Personal
reads use only the unified CAS coordinator and this adapter's independent
DPAPI session/store/lock.  No write endpoint, no form/attachment/third-party
page, and no ID walking exists.
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

# A saved account that cannot be used must not make the public catalog
# unavailable.  These are local/session/auth failures only: upstream 5xx and
# schema changes are reported instead of being hidden by a public fallback.
AUTH_FALLBACK_CODES = {
    "auth_required",
    "auth_expired",
    "auth_in_progress",
    "credential_changed",
    "identity_mismatch",
    "user_action_required",
    "local_error",
}
GUIDE_FALLBACK_CODES = {
    *AUTH_FALLBACK_CODES,
    "access_denied",
}


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
        if code in {
            "credential_missing",
            "credential_reconfigure_required",
            "credential_unreadable",
            "cas_invalid_credentials",
            "cas_login_rejected",
        }:
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

    @staticmethod
    def _fallback_next_action(error: ServicePortalError | None) -> str:
        if error is None:
            return "configure_account_or_restore_session"
        if error.action:
            return error.action
        return {
            "identity_mismatch": "service_portal_clear",
            "credential_changed": "service_portal_clear",
            "auth_expired": "service_portal_auto_auth",
            "auth_in_progress": "retry_later",
            "user_action_required": "service_portal_unlock",
            "local_error": "retry_later",
        }.get(error.code, "service_portal_auto_auth")

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

        authenticated = False
        auth_error: ServicePortalError | None = None
        result: dict | None = None
        if self.auto_auth is not None:
            try:
                current = await asyncio.to_thread(self._binding)
                if current.configured:
                    result, _session = await self._with_session(
                        lambda client, _binding: client.catalog_search(
                            keyword=keyword, page=page, page_size=page_size
                        )
                    )
                    authenticated = True
            except ServicePortalError as error:
                if error.code not in AUTH_FALLBACK_CODES:
                    raise
                auth_error = error
        if result is None:
            async with self._network_gate:
                result = await asyncio.to_thread(
                    self._search_sync, keyword, page, page_size
                )

        payload = self._base(
            "ok",
            anonymous=not authenticated,
            query={"keyword": keyword, "page": page, "page_size": page_size},
        )
        catalog_warnings = list(result.get("warnings", []))
        payload.update({key: value for key, value in result.items() if key != "warnings"})
        payload["warnings"].extend(catalog_warnings)
        payload["public_catalog_only"] = not authenticated
        payload["catalog_source"] = "apps/list"
        catalog = result.get("catalog") or {}
        if not authenticated:
            payload["warnings"].append(
                "public_catalog_only: 当前只检索匿名公开目录；这不是完整本人可见目录，"
                "可能缺少登录后才出现的请销假等事项。"
            )
            if auth_error is not None:
                payload["warnings"].append(
                    f"saved_account_unavailable: 已保存账号本次不可用（{auth_error.code}），"
                    "已退回公开目录。"
                )
            payload["next_action"] = self._fallback_next_action(auth_error)
        elif result.get("total") == 0:
            payload["warnings"].append(
                "not_found_in_current_identity_catalog: 只表示当前账号可见目录未命中，"
                "不代表全校或其他身份不存在该事项。"
            )
        with self._department_lock:
            for item in result["items"]:
                if item.get("department"):
                    self._departments[item["id"]] = item["department"]
        payload["coverage"] = {
            "source_system": "service_portal",
            "scope": "authenticated_catalog" if authenticated else "public_catalog",
            "catalog_source": "apps/list",
            "catalog_total": catalog.get("total"),
            "searchable_total": catalog.get("searchable_total"),
            "folder_count": catalog.get("folder_count"),
            "duplicate_id_count": catalog.get("duplicate_id_count"),
            "duplicate_service_id_count": catalog.get("duplicate_service_id_count"),
            "catalog_complete_for_source": catalog.get("complete") is True,
            "identity_visible_catalog": authenticated,
            "identity_scope_complete": authenticated
            and catalog.get("complete") is True,
            "public_catalog_only": not authenticated,
            "all_identities": False,
            "school_wide": False,
            "keyword_matching": "local_catalog",
            "matched_total": result.get("total"),
            "page": page,
            "page_size": page_size,
            "pages_total": result.get("pages_total"),
            "page_in_range": result.get("page_in_range"),
            "complete_for_page": result.get("complete_for_page"),
            "complete_for_scope": result.get("complete_for_scope"),
            "has_more": result.get("has_more"),
            "next_page": result.get("next_page"),
            "auth_error": auth_error.code if auth_error else None,
            "auth_action": auth_error.action if auth_error else None,
            "scope_note": (
                "登录时覆盖当前账号可见目录；匿名目录不是完整个人可见目录，"
                "也不能代表全校或其他身份。"
            ),
        }
        return payload

    def _search_sync(self, keyword: str | None, page: int, page_size: int) -> dict:
        client = self._client(None)
        try:
            return client.catalog_search(
                keyword=keyword, page=page, page_size=page_size
            )
        finally:
            client.close()

    async def guide(self, service_id: int) -> dict:
        if type(service_id) is not int or service_id < 1:
            raise ServicePortalError("invalid_input", "service_id 必须是正整数。")
        with self._department_lock:
            department = self._departments.get(service_id)
        authenticated = False
        auth_error: ServicePortalError | None = None
        result: dict | None = None
        if self.auto_auth is not None:
            try:
                current = await asyncio.to_thread(self._binding)
                if current.configured:
                    result, _session = await self._with_session(
                        lambda client, _binding: client.guide(
                            service_id, department=department
                        )
                    )
                    authenticated = True
            except ServicePortalError as error:
                if error.code not in GUIDE_FALLBACK_CODES:
                    raise
                auth_error = error
        if result is None:
            async with self._network_gate:
                result = await asyncio.to_thread(
                    self._guide_sync, service_id, department
                )
        payload = self._base(
            "ok", anonymous=not authenticated, query={"service_id": service_id}
        )
        guide_warnings = list(result.get("warnings", []))
        payload.update({key: value for key, value in result.items() if key != "warnings"})
        payload["warnings"].extend(guide_warnings)
        payload["public_guide_only"] = not authenticated
        if department is None:
            payload["warnings"].append("department_not_cached")
        if not authenticated and auth_error is not None:
            payload["warnings"].append(
                f"saved_account_unavailable: 已保存账号本次不可用（{auth_error.code}），"
                "已退回公开指南。"
            )
            payload["warnings"].append(
                "public_guide_only: 公开指南可能不包含仅登录可见的完整内容。"
            )
            payload["next_action"] = self._fallback_next_action(auth_error)
        payload["coverage"] = {
            "source_system": "service_portal",
            "scope": "authenticated_guide" if authenticated else "public_guide",
            "guide_source": "official_detail",
            "identity_visible_catalog": authenticated,
            "identity_scope_complete": authenticated,
            "public_guide_only": not authenticated,
            "all_identities": False,
            "auth_error": auth_error.code if auth_error else None,
            "auth_action": auth_error.action if auth_error else None,
            "scope_note": (
                "已保存账号可用时按同一账号读取指南；否则退回公开指南，"
                "不得把公开指南当作登录后完整内容。"
            ),
        }
        return payload

    def _guide_sync(self, service_id: int, department: str | None) -> dict:
        client = self._client(None)
        try:
            return client.guide(service_id, department=department)
        finally:
            client.close()

    async def my_todos(self, *, page: int = 1, page_size: int = 20) -> dict:
        if not 1 <= page <= MAX_PAGE or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ServicePortalError("invalid_input", "分页参数超出范围。")
        result, _session = await self._with_session(
            lambda client, current: client.todos(
                current.account_hash, page=page, page_size=page_size
            )
        )
        payload = self._base(
            "ok",
            query={"page": page, "page_size": page_size, "status": 1},
        )
        payload.update(result)
        if not result.get("page_in_range", True):
            payload["warnings"].append("page_out_of_range: 本页超出当前待办范围。")
        if result.get("total", 0) > 0 and not result.get("complete_for_scope"):
            payload["warnings"].append(
                "scope_not_fully_read: 本次只读取一页，不能声称全部待办已查完。"
            )
        payload["coverage"] = {
            "scope": "pending_at_me",
            "scope_note": (
                "已验证的 status=1 待办页；只包含当前需要本人处理的事项，"
                "不包含未读消息、其他状态或完整历史。"
            ),
            "status_filter": 1,
            "status_filter_source": "verified_request_filter",
            "all_statuses": False,
            "unread_messages_included": False,
            "page": page,
            "page_size": page_size,
            "pages_total": result.get("pages_total"),
            "page_in_range": result.get("page_in_range"),
            "complete_for_page": result.get("complete_for_page"),
            "complete_for_scope": result.get("complete_for_scope"),
            "complete_for_scope_reason": result.get("complete_for_scope_reason"),
            "has_more": result.get("has_more"),
            "next_page": result.get("next_page"),
            "remaining_after_page": result.get("remaining_after_page"),
        }
        return payload

    async def my_applications(self, *, page: int = 1, page_size: int = 20) -> dict:
        if not 1 <= page <= MAX_PAGE or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ServicePortalError("invalid_input", "分页参数超出范围。")
        result, _session = await self._with_session(
            lambda client, current: client.applications(
                current.account_hash, page=page, page_size=page_size
            )
        )
        payload = self._base(
            "ok",
            query={"page": page, "page_size": page_size, "status": 1},
        )
        payload.update(result)
        if not result.get("page_in_range", True):
            payload["warnings"].append("page_out_of_range: 本页超出当前申请范围。")
        if result.get("total", 0) > 0 and not result.get("complete_for_scope"):
            payload["warnings"].append(
                "scope_not_fully_read: 本次只读取一页，不能声称全部申请已查完。"
            )
        payload["coverage"] = {
            "scope": "my_applications_in_progress",
            "scope_note": (
                "已验证的 status=1 在办申请页；可能与“待本人处理”队列不同，"
                "其中可能有等待他人处理的申请，也可能不含其他状态或完整历史。"
            ),
            "status_filter": 1,
            "status_filter_source": "verified_request_filter",
            "all_statuses": False,
            "pending_at_me_queue": False,
            "waiting_on_others_possible": True,
            "unread_messages_included": False,
            "third_party_records_marked": True,
            "page": page,
            "page_size": page_size,
            "pages_total": result.get("pages_total"),
            "page_in_range": result.get("page_in_range"),
            "complete_for_page": result.get("complete_for_page"),
            "complete_for_scope": result.get("complete_for_scope"),
            "complete_for_scope_reason": result.get("complete_for_scope_reason"),
            "has_more": result.get("has_more"),
            "next_page": result.get("next_page"),
            "remaining_after_page": result.get("remaining_after_page"),
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
