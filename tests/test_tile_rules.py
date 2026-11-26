import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tile_raster.binning import BinConfig
from tile_raster.executor import ExecuteConfig, TileExecutor
from tile_raster.mini_grid import (
    MINI_BUCKET_CAPACITY,
    build_mini_grid,
    generate_reference_artifacts,
    independent_reference_cover,
)
from tile_raster.raster import BufferCapacityError, render_serial_reference
from tile_raster.scene import build_scene
from tile_raster.tile_rules import (
    evaluate_mini_grid,
    long_chain_scope,
    rule_evidence,
    run_tile_rules_suite,
)


class MiniGridClaimTests(unittest.TestCase):
    """文档中复算的那些数字必须稳定成立。"""

    @classmethod
    def setUpClass(cls):
        cls.issues, cls.claims = evaluate_mini_grid()
        cls.mini = cls.claims["mini_grid"]

    def test_all_mini_checks_pass(self):
        failed = [i.msg for i in self.issues if not i.ok]
        self.assertEqual(failed, [])

    def test_coverage_set_and_fragment_numbers(self):
        self.assertEqual(self.mini["covered_pixel_count"], 44)
        self.assertEqual(self.mini["covered_fragment_count"], 48)
        self.assertEqual(self.mini["overdraw_extra_count"], 4)
        sums = {
            (t["tx"], t["ty"]): (t["cover_sum"], t["load_fragments"],
                                 t["covered_pixel_count"])
            for t in self.mini["tiles"]
        }
        # 手算锚点：四块瓦的覆盖计数和 5/14/9/20。
        self.assertEqual(sums[(0, 0)][0], 5)
        self.assertEqual(sums[(1, 0)][0], 14)
        self.assertEqual(sums[(0, 1)][0], 9)
        self.assertEqual(sums[(1, 1)][0], 20)
        for (cover_sum, load, pixels) in sums.values():
            self.assertEqual(cover_sum, load)
            self.assertLessEqual(pixels, cover_sum)

    def test_overflow_state_exact(self):
        by_tile = {(t["tx"], t["ty"]): t for t in self.mini["tiles"]}
        self.assertEqual(by_tile[(0, 0)]["overflow_seq"], 2)
        self.assertEqual(by_tile[(1, 0)]["overflow_seq"], 2)
        self.assertEqual(by_tile[(1, 1)]["overflow_seq"], 4)
        self.assertIsNone(by_tile[(0, 1)]["overflow_seq"])
        heavy = by_tile[(1, 1)]
        self.assertEqual(heavy["ordered_ids"], [0, 3, 4, 5, 6])
        self.assertEqual(heavy["continuation_chunks"], [[4, 5], [6]])
        self.assertEqual(heavy["status"], "overflow-continuation")
        seqs = {(e["tile"][0], e["tile"][1]): e["seq"]
                for e in self.mini["overflow_events"]}
        self.assertEqual(seqs[(1, 1)], 4)

    def test_invariance_block_true(self):
        inv = self.mini["invariance"]
        for key, value in inv.items():
            self.assertTrue(value, key)

    def test_independent_reference_matches(self):
        scene = build_mini_grid()
        ref, _ = render_serial_reference(scene)
        self.assertEqual(independent_reference_cover(scene), ref.cover)


class RuleEvidenceNoFalsePositiveTests(unittest.TestCase):
    def test_evidence_ok_on_heavy_and_cross_tile_scenes(self):
        cases = [
            ("midway", 96, 48, 24, 4),    # 跨块三角 + 中途溢出
            ("paper", 96, 72, 24, 6),     # 重瓦片 A/B
            ("giant", 128, 96, 16, 64),   # 超大跨箱
            ("tiny", 64, 48, 16, 2),      # 小容量逼溢出
        ]
        for name, w, h, tile, cap in cases:
            for strategy in ("continuation", "rebin"):
                scene = build_scene(name, w, h)
                cfg = BinConfig(tile, tile, bucket_capacity=cap,
                                strategy=strategy, max_bucket_capacity=20000)
                res = TileExecutor(
                    scene, cfg, ExecuteConfig("shuffle", seed=13)).run()
                ev = rule_evidence(res)
                self.assertTrue(
                    ev["ok"],
                    f"{name}/{strategy} anomalies="
                    f"{json.dumps(ev['anomalies'], ensure_ascii=False)}")

    def test_tampered_coverage_is_detected(self):
        """反伪报：手改一个覆盖计数，校验必须抓到覆盖/负载不一致。"""
        scene = build_mini_grid()
        cfg = BinConfig(4, 4, bucket_capacity=MINI_BUCKET_CAPACITY,
                        max_bucket_capacity=64)
        res = TileExecutor(scene, cfg, ExecuteConfig("serial")).run()
        self.assertTrue(rule_evidence(res)["ok"])
        res.coverage[10] += 2  # 手改“证据”，不经过光栅/负载计数
        ev = rule_evidence(res)
        self.assertFalse(ev["ok"])
        ids = {a["check_id"] for a in ev["anomalies"]}
        self.assertIn("coverage-load-per-tile", ids)
        self.assertIn("coverage-frame-total", ids)

    def test_tampered_overflow_state_is_detected(self):
        scene = build_mini_grid()
        cfg = BinConfig(4, 4, bucket_capacity=2, max_bucket_capacity=64)
        res = TileExecutor(scene, cfg, ExecuteConfig("serial")).run()
        diag = next(d for d in res.tile_diags if (d.tx, d.ty) == (1, 1))
        diag.overflow_seq = 999
        ev = rule_evidence(res)
        self.assertFalse(ev["ok"])
        self.assertIn("overflow-state-evidence",
                      {a["check_id"] for a in ev["anomalies"]})


class LongChainScopeTests(unittest.TestCase):
    def test_scope_report_ok_and_cap_boundary(self):
        report = long_chain_scope()
        self.assertTrue(report["ok"])
        by_n = {row["n"]: row for row in report["lengths"]}
        self.assertTrue(by_n[17]["rebin"]["pixel_exact"])
        self.assertEqual(by_n[33]["rebin"]["error"], "BufferCapacityError")
        self.assertTrue(by_n[33]["continuation"]["ok"])
        self.assertTrue(by_n[256]["continuation"]["ok"])

    def test_rebin_hard_cap_raises_directly(self):
        from tile_raster.tile_rules import make_chain_scene

        scene = make_chain_scene(40)
        cfg = BinConfig(8, 8, bucket_capacity=4, strategy="rebin",
                        max_bucket_capacity=32)
        with self.assertRaises(BufferCapacityError):
            TileBinner = __import__(
                "tile_raster.binning", fromlist=["TileBinner"]).TileBinner
            TileBinner(scene, cfg).bin_scene()


class CleanDirectoryEntryTests(unittest.TestCase):
    """瓦片规则校验必须能在净目录以子进程跑通，不依赖手改数据。"""

    def _run(self, args, cwd):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)

    def test_module_entry_in_clean_cwd(self):
        with tempfile.TemporaryDirectory() as clean:
            proc = self._run(
                [sys.executable, "-m", "tile_raster.tile_rules", "--no-docs"],
                clean)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(Path(clean, "tile_rules_report.json").is_file())

    def test_script_entry_in_clean_cwd(self):
        script = ROOT / "scripts" / "check_tile_rules.py"
        with tempfile.TemporaryDirectory() as clean:
            proc = self._run([sys.executable, str(script)], clean)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("RULES PASS", proc.stdout)
            self.assertTrue(
                Path(clean, "tile_rules_out",
                     "tile_rules_report.json").is_file())

    def test_root_wrapper_in_clean_cwd(self):
        wrapper = ROOT / "check_tile_rules.py"
        with tempfile.TemporaryDirectory() as clean:
            proc = self._run([sys.executable, str(wrapper), "--no-docs"], clean)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_suite_api_with_temp_outdir(self):
        with tempfile.TemporaryDirectory() as clean:
            ok, report, lines = run_tile_rules_suite(
                outdir=clean, verify_docs=False)
            self.assertTrue(ok, "\n".join(lines))
            self.assertTrue(
                (Path(clean) / "tile_rules_report.json").is_file())


class MiniGridArtifactTests(unittest.TestCase):
    def test_reference_artifacts_written(self):
        with tempfile.TemporaryDirectory() as d:
            data = generate_reference_artifacts(d)
            self.assertEqual(sum(data["coverage"]), 48)
            for name in ("mini_grid_reference.json",
                         "mini_grid_coverage.txt",
                         "mini_grid_reference.ppm",
                         "mini_grid_reference.png"):
                p = Path(d) / name
                self.assertTrue(p.is_file() and p.stat().st_size > 0, name)
            png = (Path(d) / "mini_grid_reference.png").read_bytes()
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
