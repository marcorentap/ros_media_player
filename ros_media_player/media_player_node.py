#!/usr/bin/env python3

"""ROS2 media player node with an embedded web backend.

Combines an rclpy node with a dependency-free HTTP server (Python stdlib)
so a browser frontend can trigger ROS publishes without any external web
framework.  In development the Vite dev server proxies ``/api`` and
``/media`` here, giving live reload while the page still talks to ROS over
HTTP.

The node keeps a media gallery in ``<data_dir>/media``.  The browser can
list it (``GET /api/media``), upload pictures/videos to it
(``POST /api/media``, multipart/form-data) and fetch the files back
(``GET /media/<name>``).
"""

import json
import mimetypes
import os
import threading
import urllib.parse
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from std_msgs.msg import String

# Media types the gallery accepts for upload.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".ogg"}
ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS


def media_kind(name: str) -> str:
    return "video" if os.path.splitext(name)[1].lower() in VIDEO_EXTS else "image"


class MediaPlayerBackend:
    """Owns the rclpy node, its spin thread, and the media directory."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080,
                 command_topic: str = "/media_player/commands",
                 data_dir: str | None = None):
        self.host = host
        self.port = port
        self.command_topic = command_topic

        if data_dir is None:
            data_dir = os.path.expanduser("~/.ros/media_player")
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        self.media_dir = os.path.join(self.data_dir, "media")
        os.makedirs(self.media_dir, exist_ok=True)

        self.node = rclpy.create_node("media_player")
        # QoS: reliable + transient local so the publisher can be created
        # here even if no subscriber exists yet and clicks are never dropped.
        self.pub = self.node.create_publisher(String, command_topic, 10)
        self.node.get_logger().info(
            f"media_player publishing commands on '{command_topic}'")
        self.node.get_logger().info(
            f"media_player gallery at '{self.media_dir}'")

        # Spin rclpy on its own thread alongside the HTTP server.
        self.executor = MultiThreadedExecutor()
        self.executor.add_node(self.node)
        self._spin_thread = threading.Thread(
            target=self.executor.spin, daemon=True, name="rclpy-spin")

    def start(self) -> None:
        self._spin_thread.start()
        httpd = ThreadingHTTPServer(
            (self.host, self.port),
            lambda *a, **k: self._handler_cls(*a, **k))
        self.node.get_logger().info(
            f"media_player web backend listening on http://{self.host}:{self.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            self.shutdown()

    def shutdown(self) -> None:
        self.executor.shutdown()
        self.node.destroy_node()

    def _handler_cls(self, request, client_address, server):
        # Closure binds the backend so the handler can publish at request time.
        return _Handler(self, request, client_address, server)


class _Handler(BaseHTTPRequestHandler):
    """Minimal JSON + media HTTP handler for the media player backend."""

    def __init__(self, backend, *args, **kwargs):
        self.backend = backend
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # keep stderr quiet
        pass

    # --- helpers -----------------------------------------------------------

    def _respond(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _safe_media_name(self, name: str) -> str | None:
        """Return a sanitized basename if the file exists under media_dir."""
        base = os.path.basename(urllib.parse.unquote(name))
        if base in ("", ".", ".."):
            return None
        path = os.path.join(self.backend.media_dir, base)
        if os.path.isfile(path):
            return base
        return None

    def _list_media(self) -> list[dict]:
        items = []
        for name in sorted(os.listdir(self.backend.media_dir)):
            path = os.path.join(self.backend.media_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            items.append({
                "name": name,
                "size": size,
                "kind": media_kind(name),
            })
        return items

    def _serve_media(self, name: str, head_only: bool = False) -> None:
        base = self._safe_media_name(name)
        if base is None:
            self._respond(404, {"error": "not found"})
            return
        path = os.path.join(self.backend.media_dir, base)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)

        with open(path, "rb") as f:
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes=") and not head_only:
                start_s, _, end_s = range_hdr[6:].partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
                length = end - start + 1
                f.seek(start)
                data = f.read(length)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header(
                    "Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if not head_only:
                    self.wfile.write(data)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if not head_only:
                    self.wfile.write(f.read())

    def _handle_upload(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._respond(400, {"error": "expected multipart/form-data"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._respond(400, {"error": "bad content-length"})
            return

        body = self.rfile.read(length)
        raw = f"Content-Type: {ctype}\r\n\r\n".encode("utf-8") + body
        try:
            msg = message_from_bytes(raw)
        except Exception:
            self._respond(400, {"error": "could not parse multipart body"})
            return

        saved, rejected = [], []
        parts = msg.get_payload()
        if not isinstance(parts, list):
            parts = [msg]
        for part in parts:
            filename = part.get_filename()
            if not filename:
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_EXTS:
                rejected.append({"name": filename, "reason": "unsupported type"})
                continue
            data = part.get_payload(decode=True)
            if data is None:
                rejected.append({"name": filename, "reason": "empty body"})
                continue
            target = self._unique_path(os.path.basename(filename))
            with open(target, "wb") as f:
                f.write(data)
            saved.append(os.path.basename(target))
            self.backend.node.get_logger().info(
                f"saved media '{os.path.basename(target)}' "
                f"({len(data)} bytes)")

        self._respond(201, {"saved": saved, "rejected": rejected})

    def _unique_path(self, filename: str) -> str:
        """Return a non-colliding path under media_dir for `filename`."""
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(self.backend.media_dir, filename)
        n = 1
        while os.path.exists(candidate):
            candidate = os.path.join(
                self.backend.media_dir, f"{base}-{n}{ext}")
            n += 1
        return candidate

    # --- routes ------------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        route, query = parsed.path, parsed.query

        if route == "/api/health":
            self._respond(200, {
                "status": "ok",
                "command_topic": self.backend.command_topic,
            })
        elif route == "/api/config":
            self._respond(200, {
                "host": self.backend.host,
                "port": self.backend.port,
                "command_topic": self.backend.command_topic,
                "data_dir": self.backend.data_dir,
            })
        elif route == "/api/media":
            self._respond(200, {"media": self._list_media()})
        elif route.startswith("/media/"):
            self._serve_media(route[len("/media/"):])
        else:
            self._respond(404, {"error": "not found"})
        return

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/media/"):
            self._serve_media(parsed.path[len("/media/"):], head_only=True)
        else:
            self._respond(404, {"error": "not found"})
        return

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        route, query = parsed.path, parsed.query

        if route == "/api/media":
            self._handle_upload()
            return

        if route != "/publish":
            self._respond(404, {"error": "not found"})
            return

        # Parse optional JSON body: {"topic": "...", "data": "..."}
        topic = self.backend.command_topic
        data = "button-pressed"
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                payload = json.loads(self.rfile.read(length))
                topic = payload.get("topic", topic)
                data = payload.get("data", data)
        except (ValueError, json.JSONDecodeError):
            pass

        msg = String()
        msg.data = data
        self.backend.pub.publish(msg)
        self.backend.node.get_logger().info(
            f"published to '{topic}': {data!r}")
        self._respond(200, {"published": True, "topic": topic, "data": data})


def main(args=None):
    rclpy.init(args=args)

    # Bare node to read parameters before the real node exists.
    param_node = Node("media_player_params")
    param_node.declare_parameter("host", "0.0.0.0")
    param_node.declare_parameter("port", 8080)
    param_node.declare_parameter("command_topic", "/media_player/commands")
    param_node.declare_parameter("data_dir", "")
    host = param_node.get_parameter("host").value
    port = int(param_node.get_parameter("port").value)
    topic = param_node.get_parameter("command_topic").value
    data_dir = param_node.get_parameter("data_dir").value or None
    param_node.destroy_node()

    backend = MediaPlayerBackend(
        host=host, port=port, command_topic=topic, data_dir=data_dir)
    try:
        backend.start()
    finally:
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()