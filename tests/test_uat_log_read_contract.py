import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.uat_log_read_contract import (
    DomesticUatLogQuery,
    UatLogReadContractError,
    validate_response_pagination,
    validate_authorized_url,
    validate_observed_url,
)
from collector.persistent_browser_session import read_billing_page


def query(**overrides):
    values = {
        "date_from_utc": "2026-07-29T02:00:20+00:00",
        "date_to_utc": "2026-07-29T03:01:56+00:00",
        "page": 1,
        "page_size": 74,
    }
    values.update(overrides)
    return DomesticUatLogQuery(**values)


def test_reviewed_historical_range_is_transmitted_as_unix_seconds():
    value = query()
    parsed = parse_qs(urlsplit(value.url()).query, keep_blank_values=True)
    assert parsed == {
        "p": ["1"],
        "page_size": ["74"],
        "type": ["0"],
        "token_name": [""],
        "model_name": [""],
        "request_id": [""],
        "start_timestamp": [
            str(int(datetime(2026, 7, 29, 2, 0, 20,
                             tzinfo=timezone.utc).timestamp()))
        ],
        "end_timestamp": [
            str(int(datetime(2026, 7, 29, 3, 1, 56,
                             tzinfo=timezone.utc).timestamp()))
        ],
    }
    validate_authorized_url(value.url(), value)


@pytest.mark.parametrize(
    "url",
    [
        "http://uat.weimeta.cn/api/log/self",
        "https://uat.weimeta.cn.evil.test/api/log/self",
        "https://uat.weimeta.cn/api/log/search",
        "https://user@uat.weimeta.cn/api/log/self",
    ],
)
def test_source_lookalikes_fail_closed(url):
    with pytest.raises(UatLogReadContractError, match="source_not_allowed"):
        validate_authorized_url(url, query())


def test_range_or_page_substitution_fails_closed():
    expected = query()
    changed = query(page=2)
    with pytest.raises(
        UatLogReadContractError, match="query_contract_mismatch"
    ):
        validate_authorized_url(changed.url(), expected)


def test_reviewed_page_may_use_smaller_page_size_without_expanding_scope():
    authorized = query(page_size=100)
    observed = query(page_size=20)
    accepted = validate_observed_url(observed.url(), authorized)
    assert accepted.page_size == 20
    assert accepted.start == authorized.start
    assert accepted.end == authorized.end


def test_reviewed_page_compares_transmitted_second_precision():
    authorized = query(
        page_size=100,
        date_from_utc="2026-07-29T02:00:20.810+00:00",
        date_to_utc="2026-07-29T03:01:56.833+00:00",
    )
    observed = query(page_size=20)
    accepted = validate_observed_url(observed.url(), authorized)
    assert accepted.parameters()["start_timestamp"] == authorized.parameters()["start_timestamp"]
    assert accepted.parameters()["end_timestamp"] == authorized.parameters()["end_timestamp"]


def test_reviewed_page_may_omit_only_authorized_empty_optional_filters():
    authorized = query(page_size=100)
    observed_url = query(page_size=20).url().replace(
        "&token_name=&model_name=&request_id=", "")
    accepted = validate_observed_url(observed_url, authorized)
    assert accepted.token_name == accepted.model_name == accepted.request_id == ""


@pytest.mark.parametrize("changed", [
    query(page=2, page_size=20),
    query(page_size=20, date_from_utc="2026-07-29T01:59:20+00:00"),
    query(page_size=20, model_name="unauthorized-model"),
])
def test_reviewed_page_cannot_expand_or_change_authorized_query(changed):
    with pytest.raises(UatLogReadContractError, match="query_contract_mismatch"):
        validate_observed_url(changed.url(), query(page_size=100))


def test_invalid_boundary_and_page_bounds_fail_before_network():
    with pytest.raises(UatLogReadContractError, match="range_order_invalid"):
        query(date_to_utc="2026-07-29T02:00:20+00:00")
    with pytest.raises(UatLogReadContractError, match="page_size_invalid"):
        query(page_size=101)


def test_response_pagination_must_match_reserved_page_and_detect_loop():
    expected = query(page=2, page_size=2)
    payload = {
        "success": True,
        "message": "",
        "data": {"items": [{}, {}], "page": 2, "page_size": 2, "total": 8},
    }
    first = validate_response_pagination(payload, expected)
    assert first["has_next_page"] is True
    with pytest.raises(
        UatLogReadContractError, match="pagination_loop_detected"
    ):
        validate_response_pagination(
            payload,
            expected,
            previous_envelope_sha256=first["envelope_sha256"],
        )
    with pytest.raises(
        UatLogReadContractError, match="response_pagination_mismatch"
    ):
        validate_response_pagination(
            {
                **payload,
                "data": {**payload["data"], "page": 1},
            },
            expected,
        )


def test_browser_read_uses_exact_constructed_url_with_local_fake_only():
    class Page:
        def __init__(self):
            self.url = None

        def evaluate(self, _script, url):
            self.url = url
            return {
                "status": 200,
                "contentType": "application/json; charset=utf-8",
                "text": (
                    '{"success":true,"message":"","data":'
                    '{"items":[],"page":1,"page_size":74,"total":0}}'
                ),
                "oversized": False,
            }

    page = Page()
    expected = query()
    result = read_billing_page(page, expected)
    validate_authorized_url(page.url, expected)
    assert result["http_status"] == 200
    assert result["payload"]["data"]["items"] == []


def test_reviewed_page_query_applies_range_and_uses_console_request_chain():
    expected = query()

    class Input:
        def __init__(self):
            self.actions = []

        def fill(self, value):
            self.actions.append(("fill", value))

        def press(self, value):
            self.actions.append(("press", value))

    class Button:
        def __init__(self):
            self.clicked = False
            self.first = self

        def click(self):
            self.clicked = True

    class Response:
        url = expected.url()
        status = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def text():
            return json.dumps({
                "data": {"items": [], "page": 1,
                         "page_size": expected.page_size, "total": 0},
            })

    class Expectation:
        value = Response()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Page:
        def __init__(self):
            self.start = Input()
            self.end = Input()
            self.button = Button()

        def locator(self, selector):
            if "开始日期" in selector:
                return self.start
            if "结束日期" in selector:
                return self.end
            return self.button

        @staticmethod
        def expect_response(predicate, timeout):
            assert predicate(Response())
            assert timeout == 15_000
            return Expectation()

    page = Page()
    result = read_billing_page(
        page, expected, use_reviewed_page_query=True)

    assert page.start.actions == [
        ("fill", "2026-07-29 10:00:20"), ("press", "Tab")]
    assert page.end.actions == [
        ("fill", "2026-07-29 11:01:56"), ("press", "Tab")]
    assert page.button.clicked is True
    assert result["http_status"] == 200
