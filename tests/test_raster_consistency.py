import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tile_raster.binning import BinConfig
from tile_raster.executor import ExecuteConfig, TileExecutor
from tile_raster.raster import (
    RasterTarget,
    draw_triangle,
    render_serial_reference,
)
from tile_raster.scene import build_scene


def ref_rgb(scene):
    target, _ = render_serial_reference(scene)
    out = bytearray(scene.width * scene.height * 3)
    for i in range(scene.width * scene.height):
        for c in range(3):
            v = target.color[i * 4 + c]
            v = max(0.0, min(1.0, v))
            out[i * 3 + c] = int(v * 255 + 0.5)
    return bytes(out)


class PixelConsistencyTests(unittest.TestCase):
    def test_paper_all_configs_exact(self):
        scene = build_scene("paper", 96, 72)
        expected = ref_rgb(scene)
        for tile in (8, 24):
            for cap in (2, 6, 64):
                for strategy in ("continuation", "rebin"):
                    for order in ("serial", "shuffle"):
                        cfg = BinConfig(tile_w=tile, tile_h=tile,
                                        bucket_capacity=cap, strategy=strategy,
                                        max_bucket_capacity=20000)
                        res = TileExecutor(
                            scene, cfg,
                            ExecuteConfig(work_order=order, seed=3)).run()
                        self.assertEqual(
                            res.rgb, expected,
                            f"paper t={tile} cap={cap} {strategy} {order}")
                        self.assertEqual(res.binner.verify_completeness(), [])

    def test_depth_and_dense_scenes_exact(self):
        for name, w, h in (("depth-order", 64, 48), ("dense", 96, 72),
                           ("giant", 96, 72), ("empty", 96, 72),
                           ("tiny", 64, 48)):
            scene = build_scene(name, w, h)
            expected = ref_rgb(scene)
            cfg = BinConfig(tile_w=16, tile_h=16, bucket_capacity=5,
                            strategy="continuation")
            res = TileExecutor(scene, cfg,
                               ExecuteConfig("shuffle", seed=9)).run()
            self.assertEqual(res.rgb, expected, name)

    def test_out_of_order_draw_changes_pixels(self):
        """反证次序保证有意义：逆序绘制确实得到不同图像。"""
        scene = build_scene("depth-order", 64, 48)
        forward, _ = render_serial_reference(scene)
        reversed_target = RasterTarget.create(
            0, 0, 64, 48, bg=scene.background)
        stats = None
        for tri in reversed(scene.triangles):
            stats = draw_triangle(tri, reversed_target)
        diff = sum(
            abs(forward.color[i] - reversed_target.color[i]) > 1e-9
            for i in range(0, len(forward.color), 4)
        )
        self.assertGreater(diff, 5, "逆序绘制应当产生可见差异")

    def test_quad_seam_no_double_coverage(self):
        from tile_raster.scene import Triangle, Vertex, Material

        def v(x, y):
            return Vertex(x, y, 1, 1, 1, 1, 0.5)

        for x0, y0, x1, y1 in ((10, 10, 26, 26), (5, 7, 21, 23),
                               (12, 12, 13, 13)):
            t0 = Triangle(0, (v(x0, y0), v(x1, y0), v(x1, y1)), Material())
            t1 = Triangle(1, (v(x0, y0), v(x1, y1), v(x0, y1)), Material())
            c0 = RasterTarget.create(0, 0, 40, 40)
            c1 = RasterTarget.create(0, 0, 40, 40)
            draw_triangle(t0, c0)
            draw_triangle(t1, c1)
            double = sum(1 for i in range(1600) if c0.cover[i] and c1.cover[i])
            union = sum(1 for i in range(1600) if c0.cover[i] or c1.cover[i])
            self.assertEqual(double, 0)
            self.assertEqual(union, (x1 - x0) * (y1 - y0))


if __name__ == "__main__":
    unittest.main()
