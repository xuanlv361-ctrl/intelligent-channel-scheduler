from pathlib import Path

from tools.secret_scan import scan


def test_secret_scan_detects_realistic_values_and_allows_mock_labels(tmp_path):
    (tmp_path/"bad.py").write_text('token = "sk-' + "a"*30 + '"',encoding="utf-8")
    assert scan(tmp_path)==[("bad.py",1,"openai_style_key")]
    (tmp_path/"bad.py").write_text('api_key = "test-only-not-a-real-key"',encoding="utf-8")
    assert scan(tmp_path)==[]


def test_current_versionable_tree_has_no_literal_secret():
    assert scan(Path(__file__).resolve().parents[1])==[]
