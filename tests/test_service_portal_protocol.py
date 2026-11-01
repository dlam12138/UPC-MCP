from __future__ import annotations

import json

import httpx
import pytest

from upc_mcp.service_portal_protocol import (
    ServicePortalClient,
    ServicePortalError,
    html_to_text,
    parse_applications,
    parse_catalog,
    parse_envelope,
    parse_guide,
    parse_search,
    parse_todo,
    sanitize_external_url,
)


def _search_body(**overrides):
    item = {
        "id": 603,
        "name": "优秀学生党员推荐审批表",
        "departName": "党委组织部",
        "type": 1,
        "jump_type": 1,
        "details_page": 1,
        "url": "",
        "is_folder": 0,
    }
    item.update(overrides.pop("item", {}))
    body = {"e": 0, "m": "", "d": {"data": [item], "total": 1}}
    body.update(overrides)
    return body


def test_envelope_auth_and_second_factor_are_fixed_codes():
    with pytest.raises(ServicePortalError) as exc:
        parse_envelope(b'{"e":10042,"m":"personal message"}')
    assert exc.value.code == "auth_required"
    assert "personal" not in str(exc.value)
    with pytest.raises(ServicePortalError) as exc:
        parse_envelope(b'{"e":100421}')
    assert exc.value.code == "auth_expired"
    with pytest.raises(ServicePortalError) as exc:
        parse_envelope(b'{"e":30000}')
    assert exc.value.code == "user_action_required"
    assert exc.value.action == "service_portal_unlock"


def test_envelope_rejects_non_json_and_unknown_code_without_upstream_message():
    for raw in (b"<html>login</html>", b'{"m":"no code"}', b'{"e":"x"}'):
        with pytest.raises(ServicePortalError) as exc:
            parse_envelope(raw)
        assert exc.value.code == "schema_changed"
    with pytest.raises(ServicePortalError) as exc:
        parse_envelope(b'{"e":7,"m":"raw upstream private text"}')
    assert exc.value.code == "upstream_error"
    assert "raw upstream" not in str(exc.value)


def test_search_projects_minimal_fields_and_rejects_bad_shape():
    result = parse_search(_search_body(), page=1, page_size=20)
    assert result["total"] == 1
    assert result["items"][0] == {
        "id": 603,
        "name": "优秀学生党员推荐审批表",
        "department": "党委组织部",
        "type": 1,
        "access": "native_form",
        "external_host": None,
        "folder": False,
        "id_ambiguous": False,
    }
    with pytest.raises(ServicePortalError) as exc:
        parse_search({"e": 0, "d": {"total": 0}}, page=1, page_size=20)
    assert exc.value.code == "schema_changed"
    empty = parse_search(
        {"e": 0, "d": {"data": [], "total": 0}}, page=1, page_size=20
    )
    assert empty["items"] == [] and empty["total"] == 0


def test_search_external_host_and_access_shapes():
    body = _search_body(
        item={
            "id": 346,
            "name": "研究生成绩单",
            "type": 4,
            "jump_type": 1,
            "url": "https://service5.upc.edu.cn/matterApplication?app_id=346",
        }
    )
    item = parse_search(body, page=1, page_size=20)["items"][0]
    assert item["access"] == "external_url"
    assert item["external_host"] == "service5.upc.edu.cn"


def test_guide_sanitizes_scripts_and_session_query():
    body = {
        "e": 0,
        "d": {
            "id": 603,
            "name": "事项",
            "explain": "<p>说明</p><script>alert(1)</script><p>材料要求</p>",
            "detailed_information": [
                {"label": "材料", "value": "<b>身份证</b>"},
                {"field": "person", "value": "不应出现"},
            ],
            "handle_place": "行政楼",
            "handling_times": "工作日",
            "is_charge": "否",
            "charge": "",
            "forms": [{"id": 1}],
            "start_node": [{"node_name": "提交申请"}],
            "url_pc": "https://service5.upc.edu.cn/matterApplication?app_id=346&token=secret",
        },
    }
    result = parse_guide(body, 603, department="党委组织部")
    assert result["description"] == "说明\n材料要求"
    assert result["materials"] == [{"label": "材料", "value": "身份证"}]
    assert result["department"] == "党委组织部"
    assert result["form_count"] == 1
    assert result["start_nodes"] == ["提交申请"]
    assert result["external_url"] == "https://service5.upc.edu.cn/matterApplication?app_id=346"
    assert result["external_url_query_omitted"] is True
    assert "external_url_query_omitted" in result["warnings"]
    assert "secret" not in json.dumps(result, ensure_ascii=False)


def test_guide_missing_vs_empty_materials_and_wrong_id():
    base = {"id": 603, "name": "事项", "explain": ""}
    missing = parse_guide({"e": 0, "d": base}, 603, department=None)
    assert missing["materials"] is None
    assert missing["materials_status"] == "missing"
    empty = parse_guide(
        {"e": 0, "d": {**base, "detailed_information": []}}, 603, department=None
    )
    assert empty["materials"] == []
    assert empty["materials_status"] == "empty"
    bad = {**base, "detailed_information": {}}
    with pytest.raises(ServicePortalError) as exc:
        parse_guide({"e": 0, "d": bad}, 603, department=None)
    assert exc.value.code == "schema_changed"
    with pytest.raises(ServicePortalError):
        parse_guide({"e": 0, "d": base}, 999, department=None)


def test_todo_missing_list_is_schema_error_but_empty_list_is_valid():
    with pytest.raises(ServicePortalError) as exc:
        parse_todo({"e": 0, "d": {"total": 0}}, None, page=1, page_size=20)
    assert exc.value.code == "schema_changed"
    result = parse_todo(
        {"e": 0, "d": {"list": [], "total": 0}}, "acct", page=1, page_size=20
    )
    assert result["empty"] is True
    assert result["items"] == []


def test_application_projection_keeps_empty_title_and_hides_ids_and_urls():
    item = {
        "inst_id": 999,
        "id": 1000,
        "sess_id": 123,
        "creator": 42,
        "creator_name": "某同学",
        "app_name": "",
        "thirdparty_name": "第三方事项",
        "inst_status": "进行中",
        "status": 0,
        "task_status": 0,
        "thirdparty_id": 17,
        "nodes": [],
        "percent": 0,
        "created": "2024-11-19 16:01:18",
        "thirdparty_form_url_view": "https://service5.example/path?token=secret",
        "app_id": 17,
    }
    result = parse_applications(
        {"e": 0, "d": {"list": [item], "total": 1}},
        "acct",
        page=1,
        page_size=20,
    )
    row = result["items"][0]
    assert row["title"] == "第三方事项"
    assert row["title_source"] == "thirdparty_name"
    assert row["third_party"] is True
    assert row["progress"] is None
    assert row["progress_known"] is False
    assert row["flow_node_count"] is None
    assert row["ref"] and len(row["ref"]) == 24
    serialized = json.dumps(result, ensure_ascii=False)
    for forbidden in (
        "999",
        "1000",
        "123",
        "42",
        "某同学",
        "thirdparty_form_url_view",
        "secret",
    ):
        assert forbidden not in serialized
    assert "creator" not in serialized


def test_application_nonempty_nodes_make_progress_known():
    item = {
        "inst_id": 1,
        "app_name": "原生事项",
        "inst_status": "进行中",
        "status": 0,
        "thirdparty_id": 0,
        "nodes": [{"node_name": "学院审批"}],
        "percent": 50,
    }
    row = parse_applications(
        {"e": 0, "d": {"list": [item], "total": 1}},
        "acct",
        page=1,
        page_size=20,
    )["items"][0]
    assert row["third_party"] is False
    assert row["progress"] == {"percent": 50, "node_count": 1}
    assert row["progress_known"] is True


def test_catalog_search_matches_aliases_across_full_catalog_and_paginates():
    rows = [
        {"id": 1, "name": "本科生请销假", "departName": "学生工作部"},
        {"id": 2, "name": "本科生学生证补办申请", "departName": "教务处"},
        {"id": 3, "name": "研究生请假", "departName": "研究生院"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/apps/list"
        return httpx.Response(200, json={"e": 0, "d": {"list": rows}})

    client = ServicePortalClient(transport=httpx.MockTransport(handler))
    try:
        first = client.catalog_search(keyword="请销假", page=1, page_size=1)
        second = client.catalog_search(keyword="请假", page=2, page_size=1)
    finally:
        client.close()
    assert first["total"] == 1
    assert first["items"][0]["name"] == "本科生请销假"
    assert first["catalog"]["total"] == 3
    assert second["items"] == []
    assert second["complete_for_scope"] is True
    assert second["page_in_range"] is False


def test_catalog_search_does_not_turn_upstream_failure_into_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream")

    client = ServicePortalClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ServicePortalError) as caught:
        client.catalog_search(keyword="本科生请销假", page=1, page_size=20)
    client.close()
    assert caught.value.code == "service_upstream_error"


def _public_catalog_rows() -> list[dict]:
    return [
        {
            "id": 1000 + index,
            "name": f"公开事项{index:03d}",
            "departName": "公开部门",
        }
        for index in range(196)
    ]


def _authenticated_catalog_rows() -> list[dict]:
    services = _public_catalog_rows()
    services += [
        {
            "id": 3000 + index,
            "name": f"登录事项{index:03d}",
            "departName": "登录部门",
        }
        for index in range(188)
    ]
    services[350] = {
        "id": 198,
        "name": "本科生请销假",
        "departName": "学生工作部门",
        "type": 1,
        "details_page": 1,
        "is_folder": 0,
    }
    folders = [
        {"id": 3000, "name": "维修服务", "is_folder": 1},
        {"id": 3001, "name": "用印申请", "is_folder": 1},
    ]
    return folders + services


def _catalog_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/apps/list"
        authenticated = "PHPSESSID" in request.headers.get("cookie", "")
        rows = _authenticated_catalog_rows() if authenticated else _public_catalog_rows()
        return httpx.Response(200, json={"e": 0, "d": {"list": rows}})

    return httpx.MockTransport(handler)


def test_anonymous_196_and_authenticated_386_scope_differ_and_target_is_found():
    transport = _catalog_transport()
    anonymous = ServicePortalClient(transport=transport)
    try:
        public = anonymous.catalog_search(
            keyword="本科生请销假", page=1, page_size=20
        )
    finally:
        anonymous.close()
    assert public["total"] == 0
    assert public["catalog"]["total"] == 196
    assert public["catalog"]["searchable_total"] == 196
    assert public["complete_for_scope"] is True

    jar = httpx.Cookies()
    jar.set("PHPSESSID", "synthetic", domain="service.upc.edu.cn", path="/")
    authenticated = ServicePortalClient(cookies=jar, transport=transport)
    try:
        found = authenticated.catalog_search(
            keyword="本科生请销假", page=1, page_size=20
        )
        whole_page = authenticated.catalog_search(
            keyword=None, page=18, page_size=20
        )
    finally:
        authenticated.close()
    assert found["total"] == 1
    assert found["items"][0]["id"] == 198
    assert found["items"][0]["name"] == "本科生请销假"
    assert found["items"][0]["id_ambiguous"] is False
    assert found["catalog"]["total"] == 386
    assert found["catalog"]["searchable_total"] == 384
    assert found["catalog"]["folder_count"] == 2
    assert found["catalog"]["duplicate_id_count"] == 2
    assert found["catalog"]["folder_service_id_collision_count"] == 2
    assert found["complete_for_scope"] is True
    assert whole_page["count"] == 20
    assert whole_page["has_more"] is True
    assert whole_page["next_page"] == 19
    assert whole_page["complete_for_page"] is True
    assert any(item["name"] == "本科生请销假" for item in whole_page["items"])
    assert "catalog_folders_excluded" in whole_page["warnings"]


def test_catalog_preserves_duplicate_rows_and_folder_metadata_without_fake_entries():
    rows = [
        {"id": 53, "name": "维修服务", "is_folder": 1},
        {"id": 53, "name": "真实事项甲", "departName": "部门甲"},
        {"id": 64, "name": "真实事项乙", "departName": "部门乙"},
        {"id": 64, "name": "真实事项乙副本", "departName": "部门丙"},
    ]
    result = parse_catalog({"e": 0, "d": {"list": rows}})
    assert result["total"] == 4
    assert [item["name"] for item in result["items"]] == [
        "维修服务",
        "真实事项甲",
        "真实事项乙",
        "真实事项乙副本",
    ]
    assert result["searchable_total"] == 3
    assert result["folder_count"] == 1
    assert result["duplicate_id_count"] == 2
    assert result["duplicate_row_count"] == 4
    assert result["duplicate_service_id_count"] == 1
    assert result["duplicate_service_ids"] == [64]
    assert result["folder_service_id_collision_count"] == 1
    assert result["items"][0]["id_ambiguous"] is True
    assert result["items"][1]["id_ambiguous"] is True
    assert result["items"][2]["id_ambiguous"] is True


@pytest.mark.parametrize(
    "d",
    [
        {},
        {"list": {}},
        {"list": [{"id": 1}]},
        {"list": [{"id": 0, "name": "bad"}]},
        {"list": [{"id": 1, "name": ""}]},
    ],
)
def test_catalog_rejects_broken_structure_instead_of_empty_success(d):
    with pytest.raises(ServicePortalError) as caught:
        parse_catalog({"e": 0, "d": d})
    assert caught.value.code == "schema_changed"


def test_catalog_search_keeps_duplicate_service_rows_instead_of_deduplicating():
    rows = [
        {"id": 64, "name": "真实事项乙", "departName": "部门乙"},
        {"id": 64, "name": "真实事项乙副本", "departName": "部门丙"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"e": 0, "d": {"list": rows}})

    client = ServicePortalClient(transport=httpx.MockTransport(handler))
    try:
        result = client.catalog_search(keyword=None, page=1, page_size=20)
    finally:
        client.close()
    assert result["total"] == 2
    assert [item["name"] for item in result["items"]] == [
        "真实事项乙",
        "真实事项乙副本",
    ]
    assert all(item["id_ambiguous"] is True for item in result["items"])
    assert "catalog_duplicate_service_ids" in result["warnings"]


def test_http_403_is_access_denied_not_empty_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="denied")

    client = ServicePortalClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ServicePortalError) as caught:
        client.catalog()
    client.close()
    assert caught.value.code == "access_denied"
    assert caught.value.payload()["error"]["message"].startswith("e站通拒绝访问")


def test_catalog_search_page_beyond_scope_is_explicit():
    body = {
        "e": 0,
        "d": {
            "list": [
                {"id": 1, "name": "事项甲"},
                {"id": 2, "name": "事项乙"},
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = ServicePortalClient(transport=httpx.MockTransport(handler))
    try:
        result = client.catalog_search(keyword=None, page=3, page_size=1)
    finally:
        client.close()
    assert result["total"] == 2
    assert result["items"] == []
    assert result["empty_page"] is True
    assert result["page_in_range"] is False
    assert result["complete_for_page"] is False
    assert result["complete_for_scope"] is True
    assert result["has_more"] is False
    assert result["next_page"] is None


def _todo_items(count: int, start: int = 0) -> list[dict]:
    return [
        {
            "task_id": 5000 + start + index,
            "task_name": f"待办{start + index:02d}",
            "task_status": 1,
        }
        for index in range(count)
    ]


def test_todo_pagination_is_per_page_and_never_claims_full_scope():
    first = parse_todo(
        {"e": 0, "d": {"list": _todo_items(20), "total": 45}},
        "acct",
        page=1,
        page_size=20,
    )
    assert first["total"] == 45
    assert first["count"] == 20
    assert first["pages_total"] == 3
    assert first["has_more"] is True
    assert first["next_page"] == 2
    assert first["complete_for_page"] is True
    assert first["complete_for_scope"] is False
    assert first["complete_for_scope_reason"] == "other_pages_not_included"

    second = parse_todo(
        {"e": 0, "d": {"list": _todo_items(20, 20), "total": 45}},
        "acct",
        page=2,
        page_size=20,
    )
    assert second["has_more"] is True
    assert second["complete_for_page"] is True
    assert second["complete_for_scope"] is False

    last = parse_todo(
        {"e": 0, "d": {"list": _todo_items(5, 40), "total": 45}},
        "acct",
        page=3,
        page_size=20,
    )
    assert last["has_more"] is False
    assert last["next_page"] is None
    assert last["complete_for_page"] is True
    assert last["complete_for_scope"] is False

    single = parse_todo(
        {"e": 0, "d": {"list": _todo_items(45), "total": 45}},
        "acct",
        page=1,
        page_size=50,
    )
    assert single["complete_for_page"] is True
    assert single["complete_for_scope"] is True
    assert single["complete_for_scope_reason"] == "single_page_covers_all"


def test_todo_empty_identity_and_out_of_range_page_are_distinct():
    empty = parse_todo(
        {"e": 0, "d": {"list": [], "total": 0}},
        "acct",
        page=1,
        page_size=20,
    )
    assert empty["empty"] is True
    assert empty["empty_page"] is True
    assert empty["complete_for_scope"] is True
    assert empty["complete_for_scope_reason"] == "empty_scope"

    beyond = parse_todo(
        {"e": 0, "d": {"list": [], "total": 45}},
        "acct",
        page=4,
        page_size=20,
    )
    assert beyond["empty"] is False
    assert beyond["empty_page"] is True
    assert beyond["page_in_range"] is False
    assert beyond["complete_for_page"] is False
    assert beyond["complete_for_scope"] is False


def test_application_pagination_and_structure_errors():
    apps = [
        {
            "inst_id": 7000 + index,
            "app_name": f"申请{index:02d}",
            "inst_status": "进行中",
            "status": 1,
            "thirdparty_id": 0,
        }
        for index in range(7)
    ]
    result = parse_applications(
        {"e": 0, "d": {"list": apps, "total": 25}},
        "acct",
        page=1,
        page_size=7,
    )
    assert result["total"] == 25
    assert result["count"] == 7
    assert result["has_more"] is True
    assert result["complete_for_page"] is True
    assert result["complete_for_scope"] is False
    assert result["status_filter"] == 1
    assert result["third_party_count"] == 0

    with pytest.raises(ServicePortalError) as caught:
        parse_applications(
            {"e": 0, "d": {"list": apps[:6], "total": 5}},
            "acct",
            page=1,
            page_size=7,
        )
    assert caught.value.code == "schema_changed"
    with pytest.raises(ServicePortalError) as caught:
        parse_applications(
            {"e": 0, "d": {"list": apps, "total": 100}},
            "acct",
            page=1,
            page_size=6,
        )
    assert caught.value.code == "schema_changed"


def test_todo_structure_errors_are_not_silent_empty_pages():
    with pytest.raises(ServicePortalError) as caught:
        parse_todo(
            {"e": 0, "d": {"list": _todo_items(21), "total": 100}},
            "acct",
            page=1,
            page_size=20,
        )
    assert caught.value.code == "schema_changed"
    with pytest.raises(ServicePortalError) as caught:
        parse_todo(
            {"e": 0, "d": {"list": _todo_items(6), "total": 5}},
            "acct",
            page=1,
            page_size=20,
        )
    assert caught.value.code == "schema_changed"


def test_external_url_rejects_userinfo_and_drops_non_app_query():
    assert sanitize_external_url("https://user:pass@example.com/a")[0] is None
    assert sanitize_external_url("javascript:alert(1)")[0] is None
    url, host, omitted = sanitize_external_url(
        "https://service5.upc.edu.cn/a?app_id=1&ticket=ST-1&x=2"
    )
    assert url == "https://service5.upc.edu.cn/a?app_id=1"
    assert host == "service5.upc.edu.cn"
    assert omitted is True


def test_html_text_removes_scripts_and_preserves_lines():
    assert html_to_text("<script>x</script><p>a</p><li>b</li>") == "a\n- b"
