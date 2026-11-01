from __future__ import annotations

import json

import pytest

from upc_mcp.service_portal_protocol import (
    ServicePortalError,
    html_to_text,
    parse_applications,
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
        parse_todo({"e": 0, "d": {"total": 0}}, None)
    assert exc.value.code == "schema_changed"
    result = parse_todo({"e": 0, "d": {"list": [], "total": 0}}, "acct")
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
    result = parse_applications({"e": 0, "d": {"list": [item], "total": 1}}, "acct")
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
    row = parse_applications({"e": 0, "d": {"list": [item], "total": 1}}, "acct")["items"][0]
    assert row["third_party"] is False
    assert row["progress"] == {"percent": 50, "node_count": 1}
    assert row["progress_known"] is True


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
