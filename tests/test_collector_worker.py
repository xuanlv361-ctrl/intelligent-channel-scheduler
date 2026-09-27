from collector.collector_worker import _exact_log_page_visible, _safe_current_url


def test_exact_log_page_requires_https_allowlisted_host_and_path():
    allowed = {"weimeta.ai"}
    target = "https://weimeta.ai/console/log"
    assert _exact_log_page_visible(target + "?page=2", target, allowed)
    assert not _exact_log_page_visible("https://weimeta.ai/console/other", target, allowed)
    assert not _exact_log_page_visible("http://weimeta.ai/console/log", target, allowed)
    assert not _exact_log_page_visible("https://evil.example/console/log", target, allowed)


def test_safe_url_removes_query_and_fragment_without_reading_storage():
    assert _safe_current_url(
        "https://weimeta.ai/console/log?token=secret#fragment", {"weimeta.ai"}
    ) == "https://weimeta.ai/console/log"


def test_worker_source_contains_no_terminal_login_or_persistent_context():
    source = open("collector/collector_worker.py", encoding="utf-8").read()
    assert "input(" not in source
    assert "launch_persistent_context" not in source
    assert "storage_state(" not in source
    assert "context.cookies(" not in source

