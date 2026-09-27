import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import import_first_week_records as mod

ROOT = Path(__file__).resolve().parents[1]


class FirstWeekImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.csv"; self.target = self.root / "target.csv"
        self.source.write_bytes((ROOT / "data" / "first_week_source_records.csv").read_bytes())

    def test_empty_target_imports_six(self):
        result = mod.execute(self.source, self.target)
        self.assertEqual(result["new_count"], 6); self.assertEqual(len(mod.read_csv(self.target)[1]), 6)

    def test_second_run_is_idempotent(self):
        mod.execute(self.source, self.target); result = mod.execute(self.source, self.target)
        self.assertEqual((result["new_count"], result["skip_count"]), (0, 6))

    def test_dry_run_does_not_write(self):
        before = self.source.read_bytes(); mod.execute(self.source, self.target, True)
        self.assertFalse(self.target.exists()); self.assertEqual(before, self.source.read_bytes())

    def test_backup_before_write(self):
        mod.execute(self.source, self.target); mod.execute(self.source, self.target)
        self.assertTrue(list(self.root.glob("target_before_first_week_import_*.csv")))

    def test_record_id_dedup_precedes_other_keys(self):
        mod.execute(self.source, self.target); result = mod.plan_import(self.source, self.target)
        self.assertEqual(result["skip_count"], 6)

    def test_polluted_row_rejected(self):
        headers, rows = mod.read_csv(self.source); rows[0]["notes"] = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"  # secret-scan: allow
        with self.source.open("w", encoding="utf-8", newline="") as fh:
            w=csv.DictWriter(fh,fieldnames=headers); w.writeheader(); w.writerows(rows)
        with self.assertRaises(mod.ImportErrorDetail): mod.plan_import(self.source, self.target)

    def test_utf8_and_bom(self):
        raw = self.source.read_text(encoding="utf-8-sig")
        for bom in (False, True):
            path = self.root / f"source_{bom}.csv"; path.write_text(raw, encoding="utf-8-sig" if bom else "utf-8")
            self.assertEqual(len(mod.read_csv(path)[1]), 6)

    def test_missing_evidence_remains_blank(self):
        result = mod.execute(self.source, self.target)
        row = result["additions"][0]
        for field in ("actual_model","channel_id","channel_name","request_id","ttft_ms"): self.assertEqual(row[field], "")


if __name__ == "__main__": unittest.main()
