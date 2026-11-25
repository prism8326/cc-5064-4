"""零依赖图像输出：PPM 与 PNG（zlib + struct，标准库）。"""
from __future__ import annotations

import struct
import zlib


def encode_ppm(width: int, height: int, rgb: bytes | bytearray) -> bytes:
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + bytes(rgb)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(
        ">I", zlib.crc32(chunk) & 0xFFFFFFFF
    )


def encode_png(width: int, height: int, rgb: bytes | bytearray) -> bytes:
    """rgb 为长度 width*height*3 的字节串，行优先。"""
    if len(rgb) != width * height * 3:
        raise ValueError(
            f"RGB 数据长度 {len(rgb)} 与 {width}x{height}x3={width * height * 3} 不符"
        )
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter type 0
        raw.extend(rgb[y * stride:(y + 1) * stride])
    idat = zlib.compress(bytes(raw), 6)
    return (
        sig
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def float_rgb_to_bytes(color: list[float] | tuple, width: int, height: int) -> bytes:
    out = bytearray(width * height * 3)
    n = width * height
    for i in range(n):
        for c in range(3):
            v = color[i * 4 + c] if len(color) >= n * 4 else color[i * 3 + c]
            if v < 0.0:
                v = 0.0
            elif v > 1.0:
                v = 1.0
            out[i * 3 + c] = int(v * 255.0 + 0.5)
    return bytes(out)


def heat_color(t: float) -> tuple[int, int, int]:
    """0..1 -> 蓝-青-绿-黄-红 热力 LUT。"""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    stops = [
        (0.00, (18, 24, 64)),
        (0.25, (30, 110, 220)),
        (0.50, (30, 200, 180)),
        (0.75, (240, 210, 60)),
        (1.00, (220, 60, 40)),
    ]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))  # type: ignore[return-value]
    return stops[-1][1]  # type: ignore[return-value]
