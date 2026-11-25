import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tile_raster.binning import BinConfig, TileBinner
from tile_raster.geometry import tile_span_for_triangle
from tile_raster.raster import BufferCapacityError
from tile_raster.benchmarks import make_growth_stress_scene
from tile_raster.scene import build_scene


class ConservativeBinningTests(unittest.TestCase):
    def test_giant_triangle_spans_every_tile(self):
        scene = build_scene("giant", 128, 96)
        cfg = BinConfig(tile_w=16, tile_h=16, bucket_capacity=64)
        binner = TileBinner(scene, cfg).bin_scene()
        self.assertEqual((binner.cols, binner.rows), (8, 6))
        # 至少一个超大三角形覆盖全部 48 个瓦片
        self.assertEqual(binner.max_span, 48)
        giant = scene.triangles[0]
        span = tile_span_for_triangle(
            giant.xs(), giant.ys(), 128, 96, 16, 16, 8, 6)
        self.assertEqual(span[4], 48)
        self.assertEqual(binner.verify_completeness(), [])

    def test_tiny_triangles_are_not_missed(self):
        scene = build_scene("tiny", 64, 48)
        cfg = BinConfig(tile_w=16, tile_h=16, bucket_capacity=8)
        binner = TileBinner(scene, cfg).bin_scene()
        # 每个三角形都至少进入一个瓦片，且完整性校验通过
        for i, tri in enumerate(scene.triangles):
            self.assertGreaterEqual(binner.spans[i][4], 1)
        self.assertEqual(binner.verify_completeness(), [])

    def test_span_is_bounded_by_screen_grid(self):
        scene = build_scene("giant", 200, 130)
        cfg = BinConfig(tile_w=32, tile_h=32)
        binner = TileBinner(scene, cfg)
        for tri in scene.triangles:
            tx0, ty0, tx1, ty1, n = tile_span_for_triangle(
                tri.xs(), tri.ys(), 200, 130, 32, 32,
                binner.cols, binner.rows)
            self.assertGreaterEqual(tx0, 0)
            self.assertGreaterEqual(ty0, 0)
            self.assertLessEqual(tx1, binner.cols)
            self.assertLessEqual(ty1, binner.rows)
            self.assertEqual(n, (tx1 - tx0) * (ty1 - ty0))

    def test_batch_and_rebin_resolve_same_ids(self):
        scene = build_scene("paper", 96, 72)
        ids_by_strategy = {}
        for strategy in ("continuation", "rebin"):
            cfg = BinConfig(tile_w=24, tile_h=24, bucket_capacity=6,
                            strategy=strategy, max_bucket_capacity=20000)
            binner = TileBinner(scene, cfg).bin_scene()
            self.assertEqual(binner.verify_completeness(), [])
            ids_by_strategy[strategy] = {
                k: binner.resolve_ordered_ids(k) for k in binner.bins
            }
        self.assertEqual(
            ids_by_strategy["continuation"], ids_by_strategy["rebin"])

    def test_hard_cap_raises_instead_of_silent_loss(self):
        scene = make_growth_stress_scene(400)
        cfg = BinConfig(tile_w=32, tile_h=32, bucket_capacity=4,
                        strategy="rebin", max_bucket_capacity=37)
        with self.assertRaises(BufferCapacityError):
            TileBinner(scene, cfg).bin_scene()

    def test_capacity_checks_cover_worst_growth_input(self):
        scene = make_growth_stress_scene(400)
        cfg = BinConfig(tile_w=32, tile_h=32, bucket_capacity=4,
                        strategy="rebin", max_bucket_capacity=20000)
        binner = TileBinner(scene, cfg).bin_scene()
        expected = sum(span[4] for span in binner.spans)
        self.assertEqual(binner.capacity_checks, expected)
        self.assertEqual(len(binner.bins[(0, 0)].ids), 400)
        # 容量链必须真实倍增到能容纳最剧烈增长
        caps = [e.new_capacity for e in binner.events]
        self.assertEqual(caps, [8, 16, 32, 64, 128, 256, 512])


if __name__ == "__main__":
    unittest.main()
