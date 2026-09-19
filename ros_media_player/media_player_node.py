#!/usr/bin/env python3

"""ROS2 media player node with an embedded web backend.

Combines an rclpy node with a dependency-free HTTP server (Python stdlib)
so a browser frontend can trigger ROS publishes without any external web
framework.  In development the Vite dev server proxies ``/api`` and
``/media`` here, giving live reload while the page still talks to ROS over
HTTP.

The node keeps a media gallery in ``<data_dir>/media``.  Files are stored
internally under a UUID name (``<data_dir>/media/<uuid>``) with their display
name and MIME type kept in a SQLite database (``<data_dir>/media.db``).  The
browser can list it (``GET /api/media``), upload pictures/videos to it
(``POST /api/media``, multipart/form-data), fetch the files back
(``GET /media/<id>``) and rename them (``POST /api/media/rename``).  Each
media item also carries a timeline (tracks + marker points) stored as JSON in
the same database, readable via ``GET /api/timeline/<id>`` and saved with
``POST /api/timeline/<id>``.
"""

import json
import mimetypes
import os
import sqlite3
import threading
import uuid
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
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
        self.db_path = os.path.join(self.data_dir, "media.db")
        self._init_db()

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

    # --- media store (SQLite metadata + UUID files) ------------------------

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._db()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS media (
                    id         TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    mime       TEXT NOT NULL,
                    size       INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            # Display names stay unique so the gallery reads like a list of
            # real files even though the backing files are UUIDs.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_media_name ON media(name)")
            # Per-media timeline state (tracks + markers) persisted as JSON.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS timeline (
                    media_id TEXT PRIMARY KEY,
                    data     TEXT NOT NULL
                )
            """)
            conn.commit()

    def list_media(self) -> list[dict]:
        with closing(self._db()) as conn:
            rows = conn.execute(
                "SELECT id, name, mime, size FROM media ORDER BY created_at ASC"
            ).fetchall()
        return [{
            "id": row["id"],
            "name": row["name"],
            "mime": row["mime"],
            "size": row["size"],
            "kind": "video" if row["mime"].lower().startswith("video/") else "image",
        } for row in rows]

    def get_media(self, mid: str):
        with closing(self._db()) as conn:
            return conn.execute(
                "SELECT id, name, mime, size FROM media WHERE id = ?", (mid,)
            ).fetchone()

    def add_media(self, mid: str, name: str, mime: str, size: int) -> None:
        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO media (id, name, mime, size, created_at) "
                "VALUES (?,?,?,?,?)",
                (mid, name, mime, size,
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()

    def rename_media(self, mid: str, new_name: str) -> bool:
        with closing(self._db()) as conn:
            cur = conn.execute(
                "UPDATE media SET name = ? WHERE id = ?", (new_name, mid))
            conn.commit()
            return cur.rowcount > 0

    def delete_media(self, mid: str) -> bool:
        """Delete a media item: its DB row, timeline, and backing file."""
        with closing(self._db()) as conn:
            cur = conn.execute(
                "DELETE FROM media WHERE id = ?", (mid,))
            conn.execute(
                "DELETE FROM timeline WHERE media_id = ?", (mid,))
            conn.commit()
        if cur.rowcount == 0:
            return False
        try:
            os.remove(os.path.join(self.media_dir, mid))
        except OSError:
            pass
        return True

    def name_exists(self, name: str) -> bool:
        with closing(self._db()) as conn:
            row = conn.execute(
                "SELECT 1 FROM media WHERE name = ?", (name,)).fetchone()
        return row is not None

    def unique_name(self, name: str) -> str:
        """Return a display name not yet in use (basename-1.ext, ...)."""
        if not self.name_exists(name):
            return name
        stem, ext = os.path.splitext(name)
        n = 1
        while self.name_exists(f"{stem}-{n}{ext}"):
            n += 1
        return f"{stem}-{n}{ext}"

    def get_timeline(self, media_id: str) -> dict | None:
        """Return the stored timeline state for a media item, or None."""
        with closing(self._db()) as conn:
            row = conn.execute(
                "SELECT data FROM timeline WHERE media_id = ?", (media_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def save_timeline(self, media_id: str, data: dict) -> None:
        """Persist a media item's full timeline state (upsert by media id)."""
        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO timeline (media_id, data) VALUES (?, ?) "
                "ON CONFLICT(media_id) DO UPDATE SET data = excluded.data",
                (media_id, json.dumps(data)))
            conn.commit()


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

    def _list_media(self) -> list[dict]:
        return self.backend.list_media()

    def _serve_media(self, mid: str, head_only: bool = False) -> None:
        mid = urllib.parse.unquote(mid)
        rec = self.backend.get_media(mid)
        if rec is None:
            self._respond(404, {"error": "not found"})
            return
        path = os.path.join(self.backend.media_dir, mid)
        if not os.path.isfile(path):
            self._respond(404, {"error": "not found"})
            return
        ctype = rec["mime"] or "application/octet-stream"
        size = rec["size"] or os.path.getsize(path)

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
            mid = str(uuid.uuid4())
            target = os.path.join(self.backend.media_dir, mid)
            with open(target, "wb") as f:
                f.write(data)
            display = self.backend.unique_name(os.path.basename(filename))
            mime = mimetypes.guess_type(display)[0] or "application/octet-stream"
            self.backend.add_media(mid, display, mime, len(data))
            saved.append({"id": mid, "name": display})
            self.backend.node.get_logger().info(
                f"saved media '{display}' ({mid}, {len(data)} bytes)")

        self._respond(201, {"saved": saved, "rejected": rejected})

    def _handle_rename(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._respond(400, {"error": "bad content-length"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._respond(400, {"error": "expected JSON body"})
            return

        mid = payload.get("id") if isinstance(payload, dict) else None
        new_name = payload.get("newName") if isinstance(payload, dict) else None
        if not mid or not new_name:
            self._respond(400, {"error": "'id' and 'newName' required"})
            return

        if self.backend.get_media(mid) is None:
            self._respond(404, {"error": "not found"})
            return

        new = os.path.basename(urllib.parse.unquote(new_name)).strip()
        if new in ("", ".", ".."):
            self._respond(400, {"error": "invalid name"})
            return
        if self.backend.name_exists(new):
            self._respond(409, {"error": "a file with that name already exists"})
            return

        try:
            self.backend.rename_media(mid, new)
        except sqlite3.IntegrityError:
            self._respond(409, {"error": "a file with that name already exists"})
            return
        self.backend.node.get_logger().info(
            f"renamed media '{mid}' -> '{new}'")
        self._respond(200, {"id": mid, "name": new})

    def _handle_timeline_get(self, media_id: str) -> None:
        rec = self.backend.get_media(media_id)
        if rec is None:
            self._respond(404, {"error": "not found"})
            return
        data = self.backend.get_timeline(media_id) or {}
        self._respond(200, {"media": media_id, "tracks": data.get("tracks", [])})

    def _handle_timeline_post(self, media_id: str) -> None:
        rec = self.backend.get_media(media_id)
        if rec is None:
            self._respond(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._respond(400, {"error": "bad content-length"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._respond(400, {"error": "expected JSON body"})
            return
        tracks = payload.get("tracks") if isinstance(payload, dict) else None
        if not isinstance(tracks, list):
            self._respond(400, {"error": "expected {tracks: [...]}"})
            return
        # Sanitize to a stable shape so the table never stores junk.
        clean = []
        for t in tracks:
            if not isinstance(t, dict):
                continue
            key = str(t.get("key", ""))
            if not key:
                continue
            color = t.get("color") or ""
            name = (t.get("name") or "").strip() or "Track"
            points = []
            for p in t.get("points", []):
                if isinstance(p, (int, float)):
                    # Legacy scalar markers (time only); center them on the frame.
                    points.append({"t": float(p), "x": 0.5, "y": 0.5})
                elif isinstance(p, dict):
                    try:
                        tt = float(p.get("t"))
                    except (TypeError, ValueError):
                        continue
                    x = p.get("x", 0.5)
                    y = p.get("y", 0.5)
                    points.append({
                        "t": tt,
                        "x": float(x) if isinstance(x, (int, float)) else 0.5,
                        "y": float(y) if isinstance(y, (int, float)) else 0.5,
                    })
            clean.append({"key": key, "name": name, "color": color, "points": points})
        self.backend.save_timeline(media_id, {"tracks": clean})
        self.backend.node.get_logger().info(
            f"saved timeline for '{media_id}' ({len(clean)} tracks, "
            f"{sum(len(c['points']) for c in clean)} markers)")
        self._respond(200, {"ok": True})

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
        elif route.startswith("/api/timeline/"):
            mid = urllib.parse.unquote(route[len("/api/timeline/"):])
            self._handle_timeline_get(mid)
        elif route.startswith("/media/"):
            self._serve_media(route[len("/media/"):])
        else:
            self._respond(404, {"error": "not found"})
        return

    def _handle_delete(self, mid: str) -> None:
        mid = urllib.parse.unquote(mid)
        if self.backend.get_media(mid) is None:
            self._respond(404, {"error": "not found"})
            return
        self.backend.delete_media(mid)
        self.backend.node.get_logger().info(f"deleted media '{mid}'")
        self._respond(200, {"ok": True})

    def do_DELETE(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        if route.startswith("/api/media/"):
            mid = route[len("/api/media/"):]
            self._handle_delete(mid)
            return
        self._respond(405, {"error": "method not allowed"})

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

        if route == "/api/media/rename":
            self._handle_rename()
            return

        if route.startswith("/api/timeline/"):
            mid = urllib.parse.unquote(route[len("/api/timeline/"):])
            self._handle_timeline_post(mid)
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