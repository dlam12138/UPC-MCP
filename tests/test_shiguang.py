# ruff: noqa: E501
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from upc_mcp.shiguang import MAX_HTML_BYTES, ShiguangClient, ShiguangError

LIST_HTML = """
<html><body>
<div class="active_2_list">
  <div class="top clear">
    <div class="li clear">
      <div class="img"><a href="/actvMng/actvMngFront!jumpActvCampViewPageForWeb.action?actvModel.activityId=111">
        <img src="/img/a.png"></a></div>
      <div class="r">
        <div class="t3"><a class="ellipsis"
          href="/actvMng/actvMngFront!jumpActvCampViewPageForWeb.action?actvModel.activityId=111">示例活动甲</a></div>
        <p>报名截止时间: 2026-11-01 00:00</p>
        <p>活动时间：2026-11-02 00:00 ~ 2026-11-03 00:00</p>
        <p>分类：思想成长</p>
        <p>地点：线上</p>
        <a class="more" href="/actvMng/actvMngFront!jumpActvCampViewPageForWeb.action?actvModel.activityId=111">查看详情</a>
      </div>
    </div>
    <div class="li clear">
      <div class="r">
        <div class="t3"><a class="ellipsis"
          href="/actvMng/actvMngFront!jumpSocialPracticeViewPageForWeb.action?actvModel.activityId=222">示例活动乙</a></div>
        <p>报名截止时间: 2026-11-05 00:00</p>
        <p>活动时间：2026-11-06 00:00 ~ 2026-11-07 00:00</p>
        <p>分类：社会实践与志愿服务</p>
      </div>
    </div>
  </div>
</div>
<div class="page-mod"><a mypage="1">1</a><a mypage="2">2</a><span>共&nbsp;2&nbsp;页</span></div>
</body></html>
"""


def make_client(handler):
    def wrapped(request):
        response = handler(request)
        content_type = response.headers.get("content-type", "")
        if response.status_code == 200 and content_type.startswith("text/plain"):
            response.headers["content-type"] = "text/html;charset=UTF-8"
        return response

    return ShiguangClient(httpx.Client(transport=httpx.MockTransport(wrapped)))


def test_activity_list_and_pagination():
    client = make_client(lambda request: httpx.Response(200, text=LIST_HTML))
    result = client.activities(page=1, online_type="002")
    assert result["status"] == "ok"
    assert result["total_pages"] == 2
    assert result["next_page"] == 2
    assert [item["title"] for item in result["items"]] == ["示例活动甲", "示例活动乙"]
    first = result["items"][0]
    assert first["activity_id"] == "111"
    assert first["kind"] == "activity"
    assert first["category"] == "思想成长"
    assert first["location"] == "线上"
    assert result["items"][1]["kind"] == "social_practice"


def test_empty_list_is_ok_with_no_items():
    client = make_client(lambda request: httpx.Response(200, text='<div class="active_2_list"></div>'))
    result = client.activities()
    assert result["status"] == "ok"
    assert result["items"] == []
    assert result["next_page"] is None


def test_cas_redirect_is_auth_required():
    response = httpx.Response(
        302,
        headers={"location": "https://cas.upc.edu.cn/cas/login?service=x"},
    )
    client = make_client(lambda request: response)
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "auth_required"


def test_session_out_json_is_session_expired():
    response = httpx.Response(
        200,
        content=json.dumps({"sessionOutFlag": "true", "sessionOutMsg": "?"}),
        headers={"content-type": "application/json;charset=ISO-8859-1"},
    )
    client = make_client(lambda request: response)
    with pytest.raises(ShiguangError) as caught:
        client.my_participations()
    assert caught.value.code == "session_expired"


def test_report_card_credits_use_hidden_inputs_not_div_or_canvas():
    html = """
    <html><body>
      <div class="divLeft"><div class="types1">思想成长类</div>
        <div class="inlineTb1">时间</div><div class="inlineTb2">代表性课程项目名称</div>
        <div class="inlineTb3">学时</div><div class="inlineTb4">备注</div>
        <div class="inlineTh1">2026-01-01</div><div class="inlineTh2">示例</div>
        <div class="inlineTh3">2</div><div class="inlineTh4"></div>
      </div>
      <canvas id="chart"></canvas>
      <input id="credit0" value="12.5"><input id="credit1" value="8">
      <input id="credit2" value="0"><input id="credit3" value="4.0">
      <input id="credit4" value="">
      <input id="credit00" value="1.00"><input id="credit01" value="0.00">
      <input id="credit02" value="2.5"><input id="credit03" value="">
      <input id="credit04" value="3">
      <input id="flag" value="3,100,0,0,5,80,,,2,50,">
      <input id="orderNum" value="5,4,3,2,1">
    </body></html>
    """
    client = make_client(lambda request: httpx.Response(200, text=html))
    result = client.credits()
    assert result["status"] == "ok"
    assert result["total_hours"] is None
    assert result["total_credits"] is None
    assert [m["module_id"] for m in result["modules"]] == [5, 4, 3, 2, 1]
    by_id = {m["module_id"]: m for m in result["modules"]}
    assert by_id[1]["credits_earned"] == "1.00"
    assert by_id[1]["hours"] == "12.5"
    assert by_id[2]["credits_earned"] == "0.00"
    assert by_id[2]["hours"] == "8"
    assert by_id[3]["hours"] == "0"
    assert by_id[4]["credits_earned"] is None
    assert by_id[5]["hours"] is None
    assert by_id[1]["rank"] == {"position": 3, "total": 100}
    assert by_id[4]["rank"] is None
    assert by_id[1]["required_credits"] is None
    assert by_id[1]["remaining_credits"] is None


def test_report_card_total_hours_is_a_page_defined_sum():
    html = """
    <html><body>
      <input id="credit0" value="12.5"><input id="credit1" value="8">
      <input id="credit2" value="0"><input id="credit3" value="4.0">
      <input id="credit4" value="6.5">
      <input id="credit00" value="1"><input id="credit01" value="2">
      <input id="credit02" value="3"><input id="credit03" value="4">
      <input id="credit04" value="5">
      <input id="flag" value="1,10,2,10,3,10,4,10,5,10">
      <input id="orderNum" value="1,2,3,4,5">
    </body></html>
    """
    client = make_client(lambda request: httpx.Response(200, text=html))
    result = client.credits()
    assert result["total_hours"] == "31.0"
    assert result["total_hours_source"] == "sum_of_page_module_hours"


def test_report_card_records_are_structured_and_exclude_student_number():
    records = {
        "gridModel": [
            {
                "createTime": "2026-01-01",
                "hourName": "示例课程甲",
                "hourNum": 2.5,
                "hourType": "001",
                "remark": "",
                "stuNo": "synthetic-student-number",
                "objId": 1,
                "detailId": 2,
                "hourBaseType": "001",
            },
            {
                "createTime": "2026-02-02",
                "hourName": "示例课程乙",
                "hourNum": 0,
                "hourType": "008",
                "remark": None,
                "stuNo": "synthetic-student-number",
                "objId": 3,
                "detailId": 4,
                "hourBaseType": "001",
            },
        ]
    }
    client = make_client(
        lambda request: httpx.Response(
            200, json=records, headers={"content-type": "application/json;charset=UTF-8"}
        )
    )
    result = client.report_card()
    assert result["status"] == "ok"
    assert result["record_count"] == 2
    assert result["pagination"]["supported"] is False
    assert result["official_export"]["server_export_implemented"] is False
    assert [item["module"] for item in result["items"]] == ["思想成长类", "文体发展类"]
    assert result["items"][0]["hours"] == 2.5
    assert result["items"][1]["hours"] == 0
    assert result["items"][0]["recognition_status"] is None
    assert "stuNo" not in json.dumps(result)
    assert "synthetic-student-number" not in json.dumps(result)
    assert "objId" not in json.dumps(result)


def test_report_card_empty_records_is_ok():
    client = make_client(
        lambda request: httpx.Response(
            200, json={"gridModel": []}, headers={"content-type": "application/json"}
        )
    )
    result = client.report_card()
    assert result["status"] == "ok"
    assert result["items"] == []
    assert result["record_count"] == 0


def test_report_card_missing_hours_is_not_zero():
    record = {
        "createTime": "2026-01-01",
        "hourName": "示例",
        "hourNum": None,
        "hourType": "001",
        "remark": None,
    }
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={"gridModel": [record]},
            headers={"content-type": "application/json"},
        )
    )
    result = client.report_card()
    assert result["items"][0]["hours"] is None


def test_report_card_structure_change_is_parse_error():
    page = '<html><body><div class="divLeft"><canvas></canvas></div></body></html>'
    client = make_client(lambda request: httpx.Response(200, text=page))
    with pytest.raises(ShiguangError) as caught:
        client.credits()
    assert caught.value.code == "parse_error"

    client = make_client(
        lambda request: httpx.Response(
            200, json={"gridModel": {}}, headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(ShiguangError) as caught:
        client.report_card()
    assert caught.value.code == "parse_error"


def test_report_card_unauthenticated_json_redirect_is_auth_required():
    client = make_client(
        lambda request: httpx.Response(
            302, headers={"location": "https://cas.upc.edu.cn/cas/login?service=x"}
        )
    )
    with pytest.raises(ShiguangError) as caught:
        client.report_card()
    assert caught.value.code == "auth_required"


def test_report_card_403_is_access_denied():
    client = make_client(lambda request: httpx.Response(403, text="forbidden"))
    with pytest.raises(ShiguangError) as caught:
        client.credits()
    assert caught.value.code == "access_denied"


def test_activity_detail_omits_contact_and_parses_fields():
    html = """
    <div class="activeD_l">
      <div class="txt"><div class="t2">示例活动甲</div><div class="djs">示例描述</div></div>
      <div class="det_3">
        <div class="li">活动分类：思想成长</div>
        <div class="li">活动地点：线上</div>
        <div class="li">学时：2</div>
        <div class="li">联 系 人：某老师</div>
        <div class="li">联系电话：00000000000</div>
      </div>
    </div>
    """
    client = make_client(lambda request: httpx.Response(200, text=html))
    result = client.activity_detail("111")
    assert result["title"] == "示例活动甲"
    assert result["description"] == "示例描述"
    assert result["fields"]["category"] == "思想成长"
    assert result["fields"]["hours"] == "2"
    assert "联 系 人" not in result["fields"]
    assert "联系电话" not in result["fields"]


def test_my_participations_uses_my_join_path():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, text=LIST_HTML)

    client = make_client(handler)
    result = client.my_participations(page=1)
    assert seen["path"] == "/actvMng/actvMngFront!jumpActvMyJoinListPageForWeb.action"
    assert result["items"][0]["title"] == "示例活动甲"


def test_invalid_page_and_activity_id():
    client = make_client(lambda request: httpx.Response(200, text=LIST_HTML))
    with pytest.raises(ShiguangError) as caught:
        client.activities(page=0)
    assert caught.value.code == "invalid_input"
    with pytest.raises(ShiguangError) as caught:
        client.activity_detail("not-a-number")
    assert caught.value.code == "invalid_input"


def test_network_timeout_is_structured():
    def handler(request):
        raise httpx.ConnectTimeout("synthetic timeout", request=request)

    client = make_client(handler)
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "network_error"


def test_non_object_json_is_upstream_error():
    client = make_client(
        lambda request: httpx.Response(
            200,
            content=json.dumps([]),
            headers={"content-type": "application/json;charset=UTF-8"},
        )
    )
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "upstream_error"


def test_http_500_is_upstream_error_not_auth():
    client = make_client(
        lambda request: httpx.Response(
            500,
            text="synthetic server error",
            headers={"content-type": "text/html;charset=UTF-8"},
        )
    )
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "upstream_error"


def test_http_403_is_access_denied_not_auth():
    client = make_client(
        lambda request: httpx.Response(
            403,
            text="synthetic forbidden",
            headers={"content-type": "text/html;charset=UTF-8"},
        )
    )
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "access_denied"


def test_login_page_is_auth_required():
    html = """
    <html><head><title>统一身份认证</title></head>
    <body><form id="loginForm"><input name="execution" value="x"></form></body></html>
    """
    client = make_client(lambda request: httpx.Response(200, text=html))
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "auth_required"


def test_card_like_nodes_without_parseable_link_are_schema_error():
    html = '<div class="active_2_list"><div class="li clear"><a class="ellipsis">no href</a></div></div>'
    client = make_client(lambda request: httpx.Response(200, text=html))
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "parse_error"


def test_unexpected_detail_html_is_parse_error():
    client = make_client(lambda request: httpx.Response(200, text="<html>not a detail</html>"))
    with pytest.raises(ShiguangError) as caught:
        client.activity_detail("111")
    assert caught.value.code == "parse_error"


def test_invalid_enums_and_overlong_keyword():
    client = make_client(lambda request: httpx.Response(200, text=LIST_HTML))
    with pytest.raises(ShiguangError) as caught:
        client.activities(keyword="x" * 41)
    assert caught.value.code == "invalid_input"
    with pytest.raises(ShiguangError) as caught:
        client.activities(online_type="999")
    assert caught.value.code == "invalid_input"
    with pytest.raises(ShiguangError) as caught:
        client.activities(base_type="999")
    assert caught.value.code == "invalid_input"


def test_keyword_is_sent_without_truncation():
    seen = {}

    def handler(request):
        seen["search"] = request.url.params.get("searchWord")
        return httpx.Response(200, text=LIST_HTML)

    keyword = "x" * 40
    client = make_client(handler)
    client.activities(keyword=keyword)
    assert seen["search"] == keyword


def test_declared_oversize_response_is_rejected_before_body_read():
    client = make_client(
        lambda request: httpx.Response(
            200,
            content=b"x",
            headers={
                "content-type": "text/html;charset=UTF-8",
                "content-length": str(MAX_HTML_BYTES + 1),
            },
        )
    )
    with pytest.raises(ShiguangError) as caught:
        client.activities()
    assert caught.value.code == "upstream_error"


def registration_html(
    activity_id="111",
    enroll_start=-60,
    enroll_end=60,
    activity_start=120,
    activity_end=240,
):
    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M"
    return f"""
    <html><body>
      <div class="activeD_l"><div class="txt"><div class="t2">示例活动</div></div></div>
      <input id="activityId" value="{activity_id}">
      <input id="enrollStartTime" value="{(now + timedelta(minutes=enroll_start)).strftime(fmt)}">
      <input id="enrollEndTime" value="{(now + timedelta(minutes=enroll_end)).strftime(fmt)}">
      <input id="activityStratTime" value="{(now + timedelta(minutes=activity_start)).strftime(fmt)}">
      <input id="activityEndTime" value="{(now + timedelta(minutes=activity_end)).strftime(fmt)}">
      <input id="onlineType" value="001">
      <a id="actvJoin" href="#">我要报名</a>
      <a id="actvJoinByCode" href="#">邀请码报名</a>
    </body></html>
    """


def test_registration_preview_and_detail_do_not_submit():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text=registration_html())

    client = make_client(handler)
    preview = client.registration_preview("111", "activity")
    assert preview["registration"]["status"] == "open"
    assert preview["registration"]["normal_supported"] is True
    assert preview["registration"]["invite_code_supported"] is True
    assert preview["registration"]["reason_required"] is False
    detail = client.activity_detail("111")
    assert detail["registration"]["status"] == "open"
    assert all(method == "GET" for method, _path in calls)


def test_registration_preview_rejects_id_mismatch_and_missing_contract():
    client = make_client(
        lambda request: httpx.Response(200, text=registration_html(activity_id="999"))
    )
    with pytest.raises(ShiguangError) as caught:
        client.registration_preview("111", "activity")
    assert caught.value.code == "parse_error"

    client = make_client(lambda request: httpx.Response(200, text="<html></html>"))
    with pytest.raises(ShiguangError) as caught:
        client.registration_preview("111", "activity")
    assert caught.value.code == "parse_error"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"enroll_start": 60, "enroll_end": 120}, "not_started"),
        ({"enroll_start": -120, "enroll_end": -60}, "closed"),
        ({"activity_start": -120, "activity_end": -60}, "finished"),
        ({"activity_start": -1, "activity_end": 120}, "activity_in_progress"),
    ],
)
def test_registration_window_states(kwargs, expected):
    client = make_client(
        lambda request: httpx.Response(200, text=registration_html(**kwargs))
    )
    preview = client.registration_preview("111", "activity")
    assert preview["registration"]["status"] == expected


def test_registration_unset_start_with_past_end_is_closed():
    html = re.sub(
        r'(id="enrollStartTime" value=")[^"]*',
        r"\1:",
        registration_html(enroll_start=-120, enroll_end=-60),
    )
    client = make_client(lambda request: httpx.Response(200, text=html))
    preview = client.registration_preview("111", "activity")
    assert preview["registration"]["status"] == "closed"


def test_social_practice_uses_activity_online_type_and_requires_reason():
    html = registration_html().replace('id="onlineType"', 'id="activityOnlineType"')
    client = make_client(lambda request: httpx.Response(200, text=html))
    preview = client.registration_preview("111", "social_practice")
    assert preview["registration"]["status"] == "open"
    assert preview["registration"]["online_type"] == "001"
    assert preview["registration"]["reason_required"] is True


def test_normal_registration_contract_and_social_reason():
    calls = []

    def handler(request):
        calls.append(
            (
                request.url.path,
                parse_qs(request.content.decode("utf-8")),
            )
        )
        return httpx.Response(
            200, json={"oper": "success"}, headers={"content-type": "application/json"}
        )

    client = make_client(handler)
    result = client.register_activity("111", "activity")
    assert result["status"] == "registered_pending_review"
    assert result["submitted"] is True
    path, data = calls[-1]
    assert path.endswith("goEnrollInForWeb.action")
    assert data == {"actvModel.activityId": ["111"]}

    result = client.register_activity("111", "social_practice", reason="申请理由")
    assert result["status"] == "registered_pending_review"
    path, data = calls[-1]
    assert path.endswith("goEnrollInSocialForWeb.action")
    assert data == {
        "actvModel.activityId": ["111"],
        "actvModel.enrollVerifyDesc": ["申请理由"],
    }


def test_invite_registration_contract():
    calls = []

    def handler(request):
        calls.append((request.url.path, parse_qs(request.content.decode("utf-8"))))
        return httpx.Response(
            200, json={"oper": "accepted"}, headers={"content-type": "application/json"}
        )

    client = make_client(handler)
    result = client.register_activity("111", "social_practice", invite_code="CODE-1")
    assert result["status"] == "registered"
    path, data = calls[-1]
    assert path.endswith("goEnrollInByCodeForWeb.action")
    assert data == {
        "actvModel.activityId": ["111"],
        "inputInviteCode": ["CODE-1"],
    }
    assert "actvModel.enrollVerifyDesc" not in data


def test_invite_registration_error_info_requires_user_action():
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={"oper": "failed", "errorInfo": "邀请码错误"},
            headers={"content-type": "application/json"},
        )
    )
    result = client.register_activity("111", "activity", invite_code="BAD")
    assert result["status"] == "requires_user_action"
    assert result["submitted"] is False
    assert result["retry_recommended"] is False
    assert result["server_oper"] is None


@pytest.mark.parametrize(
    "oper",
    [
        "unknown-" + "personal-looking-upstream-text-" * 40,
        {"nested": "personal-looking-upstream-text"},
        12345,
    ],
)
def test_unknown_oper_is_unknown_and_not_echoed(oper):
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={"oper": oper},
            headers={"content-type": "application/json"},
        )
    )
    result = client.register_activity("111", "activity")
    assert result["status"] == "submission_unknown"
    assert result["submitted"] is None
    assert result["server_oper"] is None
    assert result["verification_required"] is True
    assert "personal-looking-upstream-text" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("oper", "expected"),
    [
        ("success", "registered_pending_review"),
        ("accepted", "registered"),
        ("SincerityError", "integrity_too_low"),
        ("moreNumAll", "registration_busy"),
        ("moreNum", "quota_full"),
        ("notRequest", "eligibility_failed"),
        ("timeRepeat", "time_conflict"),
        ("moreLockFail", "registration_contention"),
        ("beyondLimit", "registration_limit"),
        ("beyondLimit1", "lecture_limit"),
        ("unknown-business-code", "submission_unknown"),
    ],
)
def test_registration_business_codes(oper, expected):
    client = make_client(
        lambda request: httpx.Response(
            200, json={"oper": oper}, headers={"content-type": "application/json"}
        )
    )
    result = client.register_activity("111", "activity")
    assert result["status"] == expected


def test_registration_timeout_is_unknown_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    client = make_client(handler)
    with pytest.raises(ShiguangError) as caught:
        client.register_activity("111", "activity")
    assert caught.value.code == "submission_unknown"
    assert calls == ["POST"]


def test_registration_server_error_is_unknown_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(500, text="synthetic error")

    client = make_client(handler)
    with pytest.raises(ShiguangError) as caught:
        client.register_activity("111", "activity")
    assert caught.value.code == "submission_unknown"
    assert calls == ["POST"]
