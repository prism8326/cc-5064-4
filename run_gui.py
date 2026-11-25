#!/usr/bin/env python3
"""瓦片光栅调试视窗的根目录便捷启动器（等价于 python3 -m tile_raster gui）。"""
from tile_raster.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["gui"]))
