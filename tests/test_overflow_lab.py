import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tile_raster.overflow_lab import run_overflow_lab
from tile_raster.scene import make_paper_overflow_scene


class OverflowLabTests(unittest.TestCase):
    def setUp(self):
        self.scene = make_paper_overflow_scene()

    def test_adjacent_tiles_overflow_at_different_seqs_and_share_paper(self):
        rep = run_overflow_lab(self.scene, seed=7)
        a = rep["A_B"]["A"]
        b = rep["A_B"]["B"]
        self.assertEqual(a["tile"], [1, 1])
        self.assertEqual(b["tile"], [2, 1])
        self.assertIsNotNone(a["overflow_seq"])
        self.assertIsNotNone(b["overflow_seq"])
        self.assertNotEqual(a["overflow_seq"], b["overflow_seq"])
        # 关键彩纸对两块都有贡献
        self.assertEqual(a["bridge_seqs_in_tile"], [15, 16])
        self.assertEqual(b["bridge_seqs_in_tile"], [15, 16])

    def test_edge_probe_reads_and_pixel_exactness(self):
        rep = run_overflow_lab(self.scene, seed=7)
        self.assertGreater(rep["edge_probe_count"], 0)
        self.assertGreater(rep["serial"]["blend_dst_reads_on_probes"], 0)
        self.assertEqual(
            rep["tiled"]["serial"]["blend_dst_reads_on_probes"],
            rep["serial"]["blend_dst_reads_on_probes"])
        self.assertTrue(rep["probe_seq_order_matches"])
        self.assertTrue(rep["safe_pixel_exact_vs_serial"])
        self.assertTrue(rep["shuffled_pixel_exact"])
        self.assertTrue(rep["recovery_pixel_exact"])
        self.assertGreaterEqual(rep["recovery_checked_tiles"], 2)

    def test_bounded_extra_buffer_reread_counts(self):
        rep1 = run_overflow_lab(self.scene, extra_buffer_capacity=1, seed=1)
        pressure = rep1["cache_pressure"]
        self.assertIn("serial", pressure)
        self.assertIn("shuffle", pressure)
        # 容量 1 时串行完成序下至少有一块需要重读 base
        self.assertGreaterEqual(
            pressure["serial"]["total_base_rereads"], 1)
        # 至少一种顺序给出每块重读计数
        self.assertTrue(pressure["serial"]["base_rereads_by_tile"])

    def test_fault_without_trusted_base_is_reported_not_wrong_pixels(self):
        rep = run_overflow_lab(
            self.scene, trusted_base_available=False, seed=7)
        self.assertIsNotNone(rep["fatal_error"])
        self.assertIn("无法恢复", rep["fatal_error"])

    def test_larger_extra_buffer_avoids_rereads(self):
        small = run_overflow_lab(self.scene, extra_buffer_capacity=1, seed=7)
        large = run_overflow_lab(self.scene, extra_buffer_capacity=8, seed=7)
        self.assertLessEqual(
            large["cache_pressure"]["serial"]["total_base_rereads"],
            small["cache_pressure"]["serial"]["total_base_rereads"])
        self.assertEqual(
            large["cache_pressure"]["serial"]["total_base_rereads"], 0)


if __name__ == "__main__":
    unittest.main()
