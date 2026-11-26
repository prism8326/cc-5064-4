"""兼容入口名单数形式，转发到 :mod:`tile_raster.maintenance`。"""
from .docs_check import *  # noqa: F401,F403
from .maintenance import main  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
