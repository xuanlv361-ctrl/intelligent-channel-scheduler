import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import build_metrics_snapshot as mod


def record(**changes):
    base = {"tested_at":"2026-07-21 10:00:00","environment":"uat","requested_model":"m","actual_model":"m","channel_id":"1","channel_name":"c","local_group":"g","currency":"CNY","data_source":"test","is_mock":"FALSE","http_status":"200","result":"pass","error_category":"","latency_ms":"10","stream":"FALSE","ttft_ms":"","input_price_per_1m":"1","output_price_per_1m":"2","cache_read_price_per_1m":"","cache_creation_price_per_1m":""}
    base.update(changes); return base


class SnapshotTests(unittest.TestCase):
    def setUp(self): self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
    def test_success_rate(self): self.assertEqual(mod.build_snapshot([record()],[])[0]["success_rate"],"1")
    def test_zero_denominator_blank(self): self.assertEqual(mod.build_snapshot([record(http_status="401",result="fail",error_category="user_authentication_error")],[{"error_category":"user_authentication_error","channel_fault":"FALSE"}])[0]["success_rate"],"")
    def test_channel_failure(self): self.assertEqual(mod.build_snapshot([record(http_status="503",result="fail",error_category="model_unavailable")],[{"error_category":"model_unavailable","channel_fault":"TRUE"}])[0]["failure_count"],"1")
    def test_small_sample_low_confidence(self): self.assertEqual(mod.build_snapshot([record()],[])[0]["confidence_level"],"low")
    def test_currency_isolated(self): self.assertEqual(len(mod.build_snapshot([record(currency="CNY"),record(currency="USD")],[])),2)
    def test_price_version_pending(self): self.assertEqual(mod.build_snapshot([record()],[])[0]["price_version"],"pending_confirmation")
    def test_missing_channel_excluded(self):
        rows=mod.build_snapshot([record(),record(channel_id="",channel_name="")],[])
        self.assertEqual(rows[0]["excluded_count"],"1")
    def test_stream_ttft_only(self): self.assertEqual(mod.build_snapshot([record(stream="TRUE",ttft_ms="20")],[])[0]["ttft_avg_ms"],"20")
    def test_byte_stability(self):
        path=Path(self.temp.name)/"snapshot.csv"; rows=mod.build_snapshot([record()],[])
        mod.write_snapshot(rows,path); first=path.read_bytes(); mod.write_snapshot(rows,path)
        self.assertEqual(first,path.read_bytes())


if __name__ == "__main__": unittest.main()
