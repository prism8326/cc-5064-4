"""瓦片光栅调试视窗的本地图形服务（仅标准库）。

图形入口：``python -m tile_raster gui``，然后浏览器打开
http://127.0.0.1:8765 （默认自动打开）。

HTTP 只负责传参/显示；所有像素、覆盖图、负载图都由 Python 瓦片执行器实际计算。
"""
from __future__ import annotations

import json
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .service import bench_payload, check_payload, lab_payload, render_payload

WEB_DIR = Path(__file__).resolve().parent / "web"

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        server_version = "TileRaster/1.0"

        def log_message(self, fmt, *args):  # 保持终端干净
            pass

        def _send_json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/health":
                self._send_json(200, {"ok": True, "service": "tile-raster-gui"})
                return
            static = _STATIC.get(path)
            if static is None:
                self._send_json(404, {"ok": False, "error": f"未知路径 {path}"})
                return
            filename, ctype = static
            fp = WEB_DIR / filename
            if not fp.is_file():
                self._send_json(404, {"ok": False, "error": f"缺少前端文件 {filename}"})
                return
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    params = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError as e:
                    raise ValueError(f"请求不是合法 JSON：{e}")

                if path == "/api/render":
                    self._send_json(200, render_payload(params))
                elif path == "/api/lab":
                    payload = lab_payload(params)
                    self._send_json(200 if payload.get("ok") else 422, payload)
                elif path == "/api/check":
                    self._send_json(200, check_payload())
                elif path == "/api/bench":
                    self._send_json(200, bench_payload())
                else:
                    self._send_json(404, {"ok": False, "error": f"未知 API {path}"})
            except ValueError as e:
                self._send_json(400, {
                    "ok": False, "error_type": "InvalidParameter",
                    "error": str(e),
                })
            except Exception as e:  # 界面需要看到真实异常
                self._send_json(500, {
                    "ok": False, "error_type": type(e).__name__,
                    "error": str(e),
                    "traceback": traceback.format_exc(limit=4),
                })

    return Handler


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _make_handler())
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> str:
    httpd = create_server(host, port)
    url = f"http://{host}:{httpd.server_address[1]}/"
    if open_browser:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass
    print(f"瓦片光栅调试视窗已启动：{url}")
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return url
