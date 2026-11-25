import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tile_raster.binning import BinConfig, TileBinner
from tile_raster.executor import ExecuteConfig, TileExecutor
from tile_raster.raster import RasterTarget, draw_triangle, render_serial_reference
from tile_raster.scene import make_midway_scene

TILE = 24
CAP = 4


def cfg(strategy):
    return BinConfig(tile_w=TILE, tile_h=TILE, bucket_capacity=CAP,
                     strategy=strategy, max_bucket_capacity=4096)


def serial_rgb(scene):
    target, _ = render_serial_reference(scene)
    out = bytearray(scene.width * scene.height * 3)
    for i in range(scene.width * scene.height):
        for c in range(3):
            v = max(0.0, min(1.0, target.color[i * 4 + c]))
            out[i * 3 + c] = int(v * 255 + 0.5)
    return bytes(out)


class MidwayOverflowTests(unittest.TestCase):
    """跨多瓦图元在其跨度的中途瓦片顶爆候选桶。"""

    def setUp(self):
        self.scene = make_midway_scene()
        self.bridge = self.scene.triangles[-1]
        self.assertEqual(self.bridge.label, "midway-bridge")

    def test_bridge_spans_three_tiles_and_overflows_second_in_both(self):
        for strategy in ("continuation", "rebin"):
            binner = TileBinner(self.scene, cfg(strategy)).bin_scene()
            span = binner.spans[self.bridge.seq]
            tx0, ty0, tx1, ty1, n = span
            self.assertEqual(n, 3, strategy)
            self.assertEqual((tx0, ty0, tx1, ty1), (0, 0, 3, 1))

            midway = [e for e in binner.events if e.midway_in_primitive]
            self.assertEqual(len(midway), 1, strategy)
            ev = midway[0]
            # 跨度内第 2 个瓦片正是 (1,0)：先处理 (0,0)，再到 (1,0) 时溢出
            self.assertEqual((ev.tx, ev.ty), (1, 0))
            self.assertEqual(ev.span_index, 2)
            self.assertEqual(ev.primitive_span_tiles, 3)
            self.assertEqual(ev.seq, self.bridge.seq)
            self.assertEqual(binner.verify_completeness(), [])

    def test_bridge_has_real_coverage_in_multiple_tiles(self):
        """不是只进候选桶：bridge 经真实边函数在 ≥2 个瓦片内覆盖像素。"""
        target = RasterTarget.create(
            0, 0, self.scene.width, self.scene.height,
            bg=self.scene.background)
        stats = draw_triangle(self.bridge, target)
        self.assertGreater(stats.covered, 0)
        per_tile = set()
        for y in range(self.scene.height):
            for x in range(self.scene.width):
                if target.cover[y * self.scene.width + x]:
                    per_tile.add((x // TILE, y // TILE))
        self.assertIn((1, 0), per_tile)
        self.assertGreaterEqual(len(per_tile), 2)

    def test_strategies_resolve_same_full_order(self):
        orders = {}
        for strategy in ("continuation", "rebin"):
            binner = TileBinner(self.scene, cfg(strategy)).bin_scene()
            orders[strategy] = {
                key: binner.resolve_ordered_ids(key) for key in binner.bins
            }
        self.assertEqual(orders["continuation"], orders["rebin"])
        # 中途溢出的瓦片最终顺序必须严格按 seq，且包含 bridge
        r_tile = orders["continuation"][(1, 0)]
        self.assertEqual(r_tile, sorted(r_tile))
        self.assertIn(self.bridge.seq, r_tile)
        self.assertEqual(len(r_tile), CAP + 1)

    def test_continuation_batches_and_rebin_copies_are_accounted(self):
        cb = TileBinner(self.scene, cfg("continuation")).bin_scene()
        key = (1, 0)
        b = cb.bins[key]
        self.assertIsNotNone(b.continuation_cursor)
        chunks = list(cb.iter_continuation_chunks(key))
        # 桶里已有 CAP 个前缀，bridge 等剩余 id 必须能分批补全且不重复
        yielded = [sid for chunk in chunks for sid in chunk]
        full = cb.resolve_ordered_ids(key)
        stored = list(b.ids)
        self.assertEqual(sorted(stored + yielded), sorted(full))
        self.assertEqual(len(set(yielded)), len(yielded))

        rb = TileBinner(self.scene, cfg("rebin")).bin_scene()
        rb.report()
        tile = rb.bins[key]
        self.assertGreaterEqual(tile.rebins, 1)
        self.assertGreater(tile.rebin_copies, 0)
        self.assertIn(self.bridge.seq, tile.ids)

    def test_pixels_exact_vs_serial_for_both_strategies_and_orders(self):
        expected = serial_rgb(self.scene)
        for strategy in ("continuation", "rebin"):
            for order in ("serial", "shuffle"):
                result = TileExecutor(
                    self.scene, cfg(strategy),
                    ExecuteConfig(work_order=order, seed=11)).run()
                self.assertEqual(
                    result.rgb, expected, f"{strategy}/{order}")
                self.assertEqual(result.binner.verify_completeness(), [])


if __name__ == "__main__":
    unittest.main()
