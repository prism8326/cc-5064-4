"""可复算的“小网格”基准场景与独立参考绘制。

设计是刻意的小：8×8 屏幕、4×4 瓦片（2×2 共四块）、显式 ``bucket_capacity=2``
与 ``max_bucket_capacity=64``——容量限制与溢出处理全部是显式参数，绝不借用
``BinConfig`` 的默认值把它们藏起来。

几何（seq 即提交序号）::

    seq0 mini-cross  一个横跨全部 4 块瓦的大三角
    seq1/2 quad-a/b  拼成 [2,6)×[0,2) 的轴对齐四边形，共享边跨过 x=4 瓦界
    seq3..6 px-*     重瓦 H=(1,1) 内部 2×2 像素上的四个单像素三角

文档 ``docs/tile-rules.md`` 中机器可读的覆盖集合 / 溢出状态声明由
:func:`canonical_claims` 生成，并由 ``tile_raster.tile_rules`` 与
``tile_raster.maintenance`` 复算核对。

本模块不依赖任何手改数据：一切集合都从 :func:`build_mini_grid` 的代码几何
现场计算。:func:`independent_reference_cover` 是一份独立编写的暴力逐像素
参考（同样的 1/256 定点 + top-left 约定），用于和
:func:`tile_raster.raster.render_serial_reference` 互证。
"""
from __future__ import annotations

from pathlib import Path

from .scene import Scene

MINI_WIDTH = 8
MINI_HEIGHT = 8
MINI_TILE_W = 4
MINI_TILE_H = 4
MINI_COLS = MINI_WIDTH // MINI_TILE_W
MINI_ROWS = MINI_HEIGHT // MINI_TILE_H

# 显式容量：小网格就是要在这些数值下发生溢出，不能用默认 cap=8。
MINI_BUCKET_CAPACITY = 2
MINI_MAX_BUCKET_CAPACITY = 64
MINI_STRATEGY = "continuation"

# 重瓦片：四个单像素三角所在的 (tx, ty)。
HEAVY_TILE = (1, 1)
SINGLE_PIXELS = ((5, 5), (6, 5), (5, 6), (6, 6))

SCHEMA_VERSION = "tile-rules-claims/v1"


def build_mini_grid() -> Scene:
    """构造确定性的 8×8 小网格场景（无随机量）。"""
    s = Scene(
        "mini-grid", MINI_WIDTH, MINI_HEIGHT,
        background=(0.05, 0.06, 0.09),
        description="8×8 / tile4 / cap2 的瓦片规则小网格：跨四瓦三角 + 跨块共边四边形 + 重瓦 2×2 overdraw。",
    )
    # seq0：横跨全部四块瓦的大三角（形状经独立参考逐像素确认）。
    s.add_triangle(
        (-1, 7), (8, 0), (9, 8),
        (0.91, 0.30, 0.24, 1.0), label="mini-cross",
    )
    # seq1/2：[2,6)×[0,2) 四边形的两个三角，共享对角线，union 恰为 8 像素，
    # 且跨过竖直瓦界 x=4（x=2,3 在 (0,0)，x=4,5 在 (1,0)）。
    s.add_triangle(
        (2, 0), (6, 0), (6, 2),
        (0.25, 0.78, 0.36, 1.0), label="quad-a",
    )
    s.add_triangle(
        (2, 0), (6, 2), (2, 2),
        (0.25, 0.78, 0.36, 1.0), label="quad-b",
    )
    # seq3..6：包住指定像素中心的紧三角，每个恰好覆盖一个像素，
    # 保守跨度也只落在重瓦 H=(1,1) 内。
    for i, (px, py) in enumerate(SINGLE_PIXELS):
        s.add_triangle(
            (px + 0.12, py + 0.15),
            (px + 0.88, py + 0.15),
            (px + 0.50, py + 0.90),
            (0.30 + 0.10 * i, 0.55, 0.95, 1.0),
            label=f"single-{px}-{py}",
        )
    return s


def owner_tile_index(x: int, y: int, tile_w: int = MINI_TILE_W,
                     tile_h: int = MINI_TILE_H, cols: int = MINI_COLS) -> int:
    """片元归属规则：像素 (x,y) 唯一归属的瓦片线性编号 ty*cols+tx。

    瓦片矩形为 ``[tx*tw, min((tx+1)*tw,W)) × [ty*th, ...)``，因此
    ``tx = x // tw``、``ty = y // th``；边缘残瓦也由同一公式覆盖。
    """
    return (y // tile_h) * cols + (x // tile_w)


def tile_owner_map(width: int = MINI_WIDTH, height: int = MINI_HEIGHT,
                   tile_w: int = MINI_TILE_W, tile_h: int = MINI_TILE_H,
                   cols: int = MINI_COLS) -> list[int]:
    """逐像素的归属瓦片编号（长度 width*height）。"""
    return [
        owner_tile_index(x, y, tile_w, tile_h, cols)
        for y in range(height) for x in range(width)
    ]


def independent_reference_cover(scene: Scene) -> list[int]:
    """独立暴力参考：逐像素中心做定点边函数 + top-left 填充约定。

    刻意不调用 :func:`tile_raster.raster.draw_triangle`，以便与正式管线互证。
    返回每像素被三角形覆盖的次数（与 ``RasterTarget.cover`` 同口径）。
    """
    scale = 256
    half = 128
    cover = [0] * scene.width * scene.height

    for tri in scene.triangles:
        pts = [
            (int(round(v.x * scale)), int(round(v.y * scale)))
            for v in tri.verts
        ]
        (ax, ay), (bx, by), (cx, cy) = pts
        area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if area == 0:
            continue
        if area < 0:
            bx, cx = cx, bx
            by, cy = cy, by

        def top_left(p0, p1) -> bool:
            (ax0, ay0), (bx0, by0) = p0, p1
            if ay0 == by0:
                return bx0 > ax0
            return by0 < ay0

        tl0 = top_left((bx, by), (cx, cy))
        tl1 = top_left((cx, cy), (ax, ay))
        tl2 = top_left((ax, ay), (bx, by))

        for y in range(scene.height):
            py = y * scale + half
            for x in range(scene.width):
                px = x * scale + half
                w0 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
                w1 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
                w2 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
                if w0 < 0 or w1 < 0 or w2 < 0:
                    continue
                if w0 == 0 and not tl0:
                    continue
                if w1 == 0 and not tl1:
                    continue
                if w2 == 0 and not tl2:
                    continue
                cover[y * scene.width + x] += 1
    return cover


def ascii_coverage(cover: list[int], width: int = MINI_WIDTH,
                   height: int = MINI_HEIGHT, tile_w: int = MINI_TILE_W,
                   tile_h: int = MINI_TILE_H) -> str:
    """生成带瓦界的 ASCII 覆盖计数图（0 显示为 ``.``）。"""
    lines = []
    for y in range(height):
        cells = []
        for x in range(width):
            c = cover[y * width + x]
            ch = "." if c == 0 else str(min(c, 9))
            if x > 0 and x % tile_w == 0:
                cells.append("|")
            cells.append(ch)
        row = " ".join(cells)
        if y > 0 and y % tile_h == 0:
            lines.append("-" * len(row))
        lines.append(row)
    return "\n".join(lines)


def write_reference_ppm(path: str | Path, cover: list[int],
                        width: int = MINI_WIDTH, height: int = MINI_HEIGHT,
                        tile_w: int = MINI_TILE_W,
                        tile_h: int = MINI_TILE_H, scale: int = 48) -> Path:
    """把参考覆盖集合绘成放大的 PPM（P6，零依赖，任何图像查看器可开）。

    暗色=未覆盖；青绿色=覆盖 1 次；越亮/越暖表示 overdraw 越高；
    红/蓝线分别标竖直/水平瓦片边界。
    """
    max_c = max(1, max(cover))
    W, H = width * scale, height * scale
    buf = bytearray()
    for py in range(H):
        gy = py // scale
        row = bytearray()
        for px_ in range(W):
            gx = px_ // scale
            c = cover[gy * width + gx]
            if c <= 0:
                r, g, b = 16, 18, 28
            else:
                t = 0.25 + 0.75 * (c / max_c)
                r = int(40 + 215 * t * (0.4 + 0.6 * t))
                g = int(200 * (1.0 - 0.35 * t))
                b = int(230 * (1.0 - t) + 60)
            if gx % tile_w == 0 and px_ % scale < 2:
                r, g, b = 230, 90, 90
            if gy % tile_h == 0 and py % scale < 2:
                r, g, b = 90, 140, 230
            row += bytes((min(255, r), min(255, g), min(255, b)))
        buf += row
    path = Path(path)
    path.write_bytes(f"P6\n{W} {H}\n255\n".encode("ascii") + bytes(buf))
    return path


def generate_reference_artifacts(outdir: str | Path) -> dict:
    """小网格生成 + 参考绘制的文件级入口，返回写出的路径字典。"""
    import json

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    scene = build_mini_grid()
    cover = independent_reference_cover(scene)

    txt = ascii_coverage(cover)
    (outdir / "mini_grid_coverage.txt").write_text(
        "mini-grid 参考覆盖计数（| 与 --- 为瓦片边界）\n\n" + txt + "\n",
        encoding="utf-8",
    )
    ppm = write_reference_ppm(outdir / "mini_grid_reference.ppm", cover)

    png_path = outdir / "mini_grid_reference.png"
    png_path.write_bytes(_scaled_png(cover, 32))

    data = {
        "schema": SCHEMA_VERSION,
        "width": MINI_WIDTH, "height": MINI_HEIGHT,
        "tile_w": MINI_TILE_W, "tile_h": MINI_TILE_H,
        "bucket_capacity": MINI_BUCKET_CAPACITY,
        "max_bucket_capacity": MINI_MAX_BUCKET_CAPACITY,
        "coverage": cover,
        "ascii": txt,
        "ppm": str(ppm),
        "png": str(png_path),
    }
    (outdir / "mini_grid_reference.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _scaled_png(cover: list[int], scale: int) -> bytes:
    from .imaging import encode_png

    max_c = max(1, max(cover))
    W, H = MINI_WIDTH * scale, MINI_HEIGHT * scale
    rgb = bytearray(W * H * 3)
    for y in range(H):
        gy = y // scale
        for x in range(W):
            gx = x // scale
            c = cover[gy * MINI_WIDTH + gx]
            if c <= 0:
                r, g, b = 16, 18, 28
            else:
                t = 0.25 + 0.75 * (c / max_c)
                r = int(60 + 195 * t)
                g = int(210 * (1 - 0.5 * t))
                b = int(235 * (1 - t * 0.7))
            if gx % MINI_TILE_W == 0 and x % scale < 2:
                r, g, b = 235, 110, 110
            if gy % MINI_TILE_H == 0 and y % scale < 2:
                r, g, b = 110, 160, 240
            o = (y * W + x) * 3
            rgb[o] = min(255, r)
            rgb[o + 1] = min(255, g)
            rgb[o + 2] = min(255, b)
    return encode_png(W, H, bytes(rgb))


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m tile_raster.mini_grid",
        description="生成 8×8 小网格并独立参考绘制覆盖集合（不依赖任何手改数据）")
    p.add_argument("--out", default="mini_grid_out",
                   help="产物目录（不存在会创建；建议传一个净目录）")
    args = p.parse_args(argv)
    data = generate_reference_artifacts(args.out)
    print(f"小网格参考产物已写入净目录：{Path(args.out).resolve()}")
    print(data["ascii"])
    print(f"覆盖片段合计={sum(data['coverage'])} "
          f"覆盖像素集合={sum(1 for c in data['coverage'] if c > 0)}")
    return 0


if __name__ == "__main__":
    import sys

    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
