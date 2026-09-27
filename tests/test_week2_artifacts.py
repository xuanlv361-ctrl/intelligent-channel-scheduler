import csv
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class ArtifactTests(unittest.TestCase):
    def setUp(self): self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
    def rows(self,name):
        with (ROOT/"data"/name).open(encoding="utf-8-sig",newline="") as fh:
            return list(csv.DictReader(fh))
    def test_taxonomy_categories_and_decisions(self):
        rows=self.rows("error_taxonomy.csv"); required={"local_command_error","user_authentication_error","request_parameter_error","platform_routing_error","upstream_authentication_error","model_unavailable","protocol_compatibility_error","model_mapping_mismatch","usage_field_mismatch","timeout","rate_limit","unknown_error"}
        self.assertEqual({r["error_category"] for r in rows},required)
        self.assertTrue(all(r[f] in {"TRUE","FALSE","pending_confirmation"} for r in rows for f in ("channel_fault","retryable","fallback_allowed")))
    def test_snapshot_dictionary_covers_fields(self):
        fields={r["field_name"] for r in self.rows("metrics_snapshot_dictionary.csv")}
        self.assertEqual(fields,set(__import__("sys").path and __import__("importlib").import_module("src.build_metrics_snapshot").FIELDS))
    def test_new_csvs_have_no_secrets(self):
        for name in ("error_taxonomy.csv","metrics_snapshot_dictionary.csv","first_week_source_records.csv"):
            text=(ROOT/"data"/name).read_text(encoding="utf-8-sig").lower()
            self.assertNotIn("authorization: bearer ",text); self.assertNotIn("sk-",text)


if __name__ == "__main__": unittest.main()
