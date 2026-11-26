import json
import os
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tile_raster.benchmarks import run_check_suite
from tile_raster.service import render_payload, validate_render_params


class ServiceTests(unittest.TestCase):
    def test_render_payload_has_real_pngs_and_diag(self):
        payload = render_payload({
            "scene": "paper", "tile": 24, "bucket_capacity": 6,
            "work_order": "shuffle", "seed": 7,
        })
        self.assertTrue(payload["ok"])
        for key in ("image_png", "coverage_png", "load_png"):
            self.assertTrue(payload[key].startswith("data:image/png;base64,"))
        metrics = payload["metrics"]
        self.assertEqual(len(metrics["tiles"]), 12)
        self.assertIn("lines", payload["diagnosis"])
        # 覆盖/负载必须来自真实计数
        self.assertGreater(metrics["total"]["covered"], 0)

    def test_empty_scene_has_zero_fragment_work(self):
        payload = render_payload({"scene": "empty"})
        self.assertEqual(payload["metrics"]["total"]["covered"], 0)

    def test_invalid_params_raise_chinese_value_error(self):
        with self.assertRaises(ValueError):
            validate_render_params({"width": 8})
        with self.assertRaises(ValueError):
            validate_render_params({"strategy": "nope"})
        with self.assertRaises(ValueError):
            validate_render_params({"bucket_capacity": 0})

    def test_check_suite_passes(self):
        ok, _, lines = run_check_suite()
        self.assertTrue(ok, "\n".join(lines))


class HttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tile_raster.server import create_server
        cls.httpd = create_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_and_static_index(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/health", timeout=5) as r:
            self.assertEqual(r.status, 200)
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/", timeout=5) as r:
            html = r.read().decode("utf-8")
            self.assertIn("瓦片光栅调试视窗", html)

    def test_http_render_and_invalid(self):
        status, data = self._post("/api/render", {"scene": "giant"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, data = self._post("/api/render", {"width": 1})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("error", data)

    def test_http_fault_lab_returns_422_with_message(self):
        status, data = self._post("/api/lab", {"fault_no_base": True})
        self.assertEqual(status, 422)
        self.assertEqual(data["error_type"], "OrderUnrecoverableError")

    def test_render_payload_carries_zero_anomaly_evidence(self):
        payload = render_payload({"scene": "midway", "tile": 24,
                                  "bucket_capacity": 4})
        self.assertIn("rule_evidence", payload)
        self.assertTrue(payload["rule_evidence"]["ok"])
        self.assertEqual(payload["anomalies"], [])
        totals = payload["rule_evidence"]["totals"]
        self.assertGreater(totals["covered_fragments"], 0)

    def test_http_tile_rules_endpoint(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/tile-rules",
            data=b"{}", headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read())
        self.assertTrue(data["ok"], "\n".join(data.get("lines", [])))
        self.assertEqual(data["failed_issues"], [])
        self.assertIn("mini_grid", data)

    def test_http_get_tile_rules_alias(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/tile-rules", timeout=180) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read())
        self.assertIn("ok", data)


if __name__ == "__main__":
    unittest.main()
