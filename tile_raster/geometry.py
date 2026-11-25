"""几何变换与保守瓦片范围计算。

屏幕坐标约定：原点在左上角，x 向右、y 向下，像素 (ix, iy) 的中心为
(ix + 0.5, iy + 0.5)。所有变换后的三角形都使用该坐标系。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

Mat3 = tuple[float, ...]  # 9 个元素，行优先


def mat3_identity() -> Mat3:
    return (1.0, 0.0, 0.0,
            0.0, 1.0, 0.0,
            0.0, 0.0, 1.0)


def mat3_translate(tx: float, ty: float) -> Mat3:
    return (1.0, 0.0, tx,
            0.0, 1.0, ty,
            0.0, 0.0, 1.0)


def mat3_scale(sx: float, sy: float | None = None) -> Mat3:
    if sy is None:
        sy = sx
    return (sx, 0.0, 0.0,
            0.0, sy, 0.0,
            0.0, 0.0, 1.0)


def mat3_rotate(rad: float) -> Mat3:
    c, s = math.cos(rad), math.sin(rad)
    return (c, -s, 0.0,
            s, c, 0.0,
            0.0, 0.0, 1.0)


def mat3_mul(a: Mat3, b: Mat3) -> Mat3:
    """返回 a * b（先应用 b 再应用 a）。"""
    out = [0.0] * 9
    for r in range(3):
        for c in range(3):
            out[r * 3 + c] = (
                a[r * 3 + 0] * b[0 * 3 + c]
                + a[r * 3 + 1] * b[1 * 3 + c]
                + a[r * 3 + 2] * b[2 * 3 + c]
            )
    return tuple(out)


def mat3_apply(m: Mat3, x: float, y: float) -> tuple[float, float]:
    w = m[6] * x + m[7] * y + m[8]
    nx = (m[0] * x + m[1] * y + m[2]) / w
    ny = (m[3] * x + m[4] * y + m[5]) / w
    return nx, ny


def compose_affine(
    tx: float = 0.0,
    ty: float = 0.0,
    rotation_deg: float = 0.0,
    sx: float = 1.0,
    sy: float | None = None,
) -> Mat3:
    """按 缩放 -> 旋转 -> 平移 组合仿射矩阵（局部坐标 -> 屏幕坐标）。"""
    m = mat3_translate(tx, ty)
    m = mat3_mul(m, mat3_rotate(math.radians(rotation_deg)))
    m = mat3_mul(m, mat3_scale(sx, sy))
    return m


def edge_function(
    ax: float, ay: float,
    bx: float, by: float,
    px: float, py: float,
) -> float:
    """有向边 a->b 的叉积 cross(b-a, p-a)。

    对统一绕序的三角形，内部点三个边函数同号。
    """
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax)


def normalize_winding(v0, v1, v2):
    """返回绕序统一为“内部边函数 >= 0”的三元组。"""
    area = edge_function(v0.x, v0.y, v1.x, v1.y, v2.x, v2.y)
    if area < 0.0:
        return v0, v2, v1, -area
    return v0, v1, v2, area


def conservative_pixel_bounds(
    xs: Sequence[float], ys: Sequence[float], width: int, height: int
) -> tuple[int, int, int, int]:
    """保守的像素下标范围 [x0, x1) × [y0, y1)。

    向外扩 1 像素并与屏幕裁剪，宁可多送入瓦片也不漏掉覆盖像素。
    对极小/亚像素三角形同样返回非空范围。
    """
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    x0 = int(math.floor(minx)) - 1
    x1 = int(math.ceil(maxx)) + 1
    y0 = int(math.floor(miny)) - 1
    y1 = int(math.ceil(maxy)) + 1
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(width, x1)
    y1 = min(height, y1)
    if x1 <= x0:
        # 落在屏幕内但宽度取整为空时，保守给出一个像素列。
        cx = int(math.floor(minx))
        cx = min(max(cx, 0), width - 1)
        x0, x1 = cx, cx + 1
    if y1 <= y0:
        cy = int(math.floor(miny))
        cy = min(max(cy, 0), height - 1)
        y0, y1 = cy, cy + 1
    return x0, x1, y0, y1


def tile_span_for_triangle(
    xs: Sequence[float],
    ys: Sequence[float],
    width: int,
    height: int,
    tile_w: int,
    tile_h: int,
    cols: int,
    rows: int,
) -> tuple[int, int, int, int, int]:
    """返回保守瓦片范围 (tx0, ty0, tx1_excl, ty1_excl, tile_count)。"""
    x0, x1, y0, y1 = conservative_pixel_bounds(xs, ys, width, height)
    tx0 = max(0, x0 // tile_w)
    ty0 = max(0, y0 // tile_h)
    tx1 = min(cols, (x1 - 1) // tile_w + 1)
    ty1 = min(rows, (y1 - 1) // tile_h + 1)
    if tx1 <= tx0:
        tx1 = min(cols, tx0 + 1)
    if ty1 <= ty0:
        ty1 = min(rows, ty0 + 1)
    count = max(0, tx1 - tx0) * max(0, ty1 - ty0)
    return tx0, ty0, tx1, ty1, count


def iter_covered_tiles(
    span: tuple[int, int, int, int, int]
) -> Iterable[tuple[int, int]]:
    tx0, ty0, tx1, ty1, _ = span
    for ty in range(ty0, ty1):
        for tx in range(tx0, tx1):
            yield tx, ty
