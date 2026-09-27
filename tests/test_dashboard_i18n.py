import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.i18n import LANGUAGE_OPTIONS, TRANSLATIONS, localize_rows, text


def test_all_languages_have_identical_translation_keys():
    assert LANGUAGE_OPTIONS == {"English": "en", "中文": "zh", "日本語": "ja"}
    english_keys = set(TRANSLATIONS["en"])
    assert english_keys
    assert set(TRANSLATIONS["zh"]) == english_keys
    assert set(TRANSLATIONS["ja"]) == english_keys


def test_table_headings_and_status_values_are_localized():
    rows = [{"channel_id": "C-1", "status": "healthy", "success_rate": 1.0}]
    assert localize_rows(rows, "zh") == [
        {"渠道ID": "C-1", "状态": "健康", "成功率": 1.0}
    ]
    assert localize_rows(rows, "ja") == [
        {"チャネルID": "C-1", "状態": "正常", "成功率": 1.0}
    ]


def test_translation_is_deterministic_and_falls_back_to_english():
    assert text("zh", "app_title") == text("zh", "app_title")
    assert text("unknown", "app_title") == TRANSLATIONS["en"]["app_title"]
