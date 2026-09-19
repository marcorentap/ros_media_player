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
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import urllib.parse
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from geometry_msgs.msg import Point, PointStamped
from sensor_msgs.msg import Image as ImageMsg
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
        # Offline preprocessed (normalized) copies, one H.264 stream per
        # (media, width, height, fps) cache key. Publishing decodes from these
        # so the live path never re-encodes, and its native frame rate matches
        # the publish rate 1:1 (see _Player).
        self.preprocessed_dir = os.path.join(self.data_dir, "preprocessed")
        os.makedirs(self.preprocessed_dir, exist_ok=True)
        self._pp_locks: dict[str, threading.Lock] = {}
        self.db_path = os.path.join(self.data_dir, "media.db")
        self._init_db()

        self.node = rclpy.create_node("media_player")
        # QoS: reliable + transient local so the publisher can be created
        # here even if no subscriber exists yet and clicks are never dropped.
        self.pub = self.node.create_publisher(String, command_topic, 10)
        # Lazily-created publishers for media frames (sensor_msgs/Image) and
        # lane-track points (geometry_msgs/PointStamped or Point), keyed by
        # topic so a configurable topic doesn't require a fixed publisher.
        self._img_pubs: dict[str, object] = {}
        self._stamped_pubs: dict[str, object] = {}
        self._plain_pubs: dict[str, object] = {}
        self._player = _Player(self)
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

    def _img_pub(self, topic):
        pub = self._img_pubs.get(topic)
        if pub is None:
            pub = self.node.create_publisher(ImageMsg, topic, 10)
            self._img_pubs[topic] = pub
        return pub

    def _stamped_pub(self, topic):
        pub = self._stamped_pubs.get(topic)
        if pub is None:
            pub = self.node.create_publisher(PointStamped, topic, 10)
            self._stamped_pubs[topic] = pub
        return pub

    def _plain_pub(self, topic):
        pub = self._plain_pubs.get(topic)
        if pub is None:
            pub = self.node.create_publisher(Point, topic, 10)
            self._plain_pubs[topic] = pub
        return pub

    def publish_image(self, topic: str, frame_id: str,
                      width: int, height: int, encoding: str, data: bytes) -> None:
        """Publish raw pixel bytes as a sensor_msgs/Image."""
        msg = ImageMsg()
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.header.frame_id = frame_id
        msg.height = int(height)
        msg.width = int(width)
        msg.encoding = encoding
        msg.is_bigendian = False
        msg.step = int(width) * (4 if encoding == "rgba8" else 3)
        msg.data = data
        self._img_pub(topic).publish(msg)
        self.node.get_logger().info(
            f"published image [{encoding} {width}x{height}] on '{topic}'")

    def publish_point(self, topic: str, frame_id: str,
                      x: float, y: float, stamped: bool) -> None:
        """Publish a lane-track point as PointStamped (or bare Point)."""
        now = self.node.get_clock().now().to_msg()
        if stamped:
            msg = PointStamped()
            msg.header.stamp = now
            msg.header.frame_id = frame_id
            msg.point.x = float(x)
            msg.point.y = float(y)
            msg.point.z = 0.0
            self._stamped_pub(topic).publish(msg)
        else:
            msg = Point()
            msg.x = float(x)
            msg.y = float(y)
            msg.z = 0.0
            self._plain_pub(topic).publish(msg)
        self.node.get_logger().info(
            f"published {'PointStamped' if stamped else 'Point'} ({x:.4f},{y:.4f}) "
            f"on '{topic}'")

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
        # Drop any preprocessed (normalized) copies of this media too.
        import glob
        for p in glob.glob(os.path.join(self.preprocessed_dir, mid + "__*.mp4")):
            try:
                os.remove(p)
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

    def _probe_video(self, path: str) -> dict:
        """Return {width, height, fps} for a video file, or {} on failure."""
        ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,avg_frame_rate",
                 "-of", "json", path],
                capture_output=True, timeout=30)
        except Exception:
            return {}
        try:
            s = json.loads(out.stdout)["streams"][0]
        except (ValueError, KeyError, IndexError):
            return {}
        w = int(s.get("width") or 0)
        h = int(s.get("height") or 0)
        fps = 0.0
        rate = s.get("avg_frame_rate")
        if rate:
            num, _, den = str(rate).partition("/")
            try:
                den = float(den) if den else 1.0
                fps = float(num) / den if den else 0.0
            except (TypeError, ValueError):
                fps = 0.0
        return {"width": w, "height": h, "fps": fps or 0.0}

    def preprocess(self, media_id: str, base_path: str,
                   width: int, height: int, fps: float) -> dict:
        """Normalize a video off the request path into a cached H.264 stream
        at the target publish size/fps, and return the file the player should
        decode/publish from: {"path", "width", "height", "fps",
        "preprocessed"}.

        The cache key is derived from (media_id, size, fps), so visiting a
        video or changing a publish setting re-triggers a rebuild without
        touching the original. width/height == 0 keep natural size and
        fps <= 0 keeps the source frame rate, so when those are unchanged the
        original file is reused directly (nothing to gain from a duplicate
        re-encode). Falls back to the original (live decode) when ffmpeg or a
        probe is unavailable.
        """
        probe = self._probe_video(base_path)
        natural_w = probe.get("width", 0)
        natural_h = probe.get("height", 0)
        native_fps = probe.get("fps", 0.0)
        if not natural_w or not natural_h or not native_fps:
            self.node.get_logger().warning(
                f"could not probe '{media_id}' for preprocessing")
            return {"path": base_path, "width": int(width), "height": int(height),
                    "fps": float(fps), "preprocessed": False}
        w = int(width or natural_w)
        h = int(height or natural_h)
        target_fps = max(1.0, float(fps)) if (fps and fps > 0) else native_fps
        # Nothing to change: publishing from the original at native rate is
        # already 1:1, so skip the pointless duplicate transcode.
        if (w == natural_w and h == natural_h
                and abs(target_fps - native_fps) < 1e-6):
            return {"path": base_path, "width": w, "height": h,
                    "fps": native_fps, "preprocessed": False}

        key = f"{media_id}__{w}x{h}_{target_fps:.6g}.mp4"
        dst = os.path.join(self.preprocessed_dir, key)
        if os.path.isfile(dst):
            return {"path": dst, "width": w, "height": h,
                    "fps": target_fps, "preprocessed": True}

        # Serialize builds per media so a background visit-trigger and a play
        # racing in don't transcode+overwrite the same cache file together.
        lock = self._pp_locks.setdefault(media_id, threading.Lock())
        with lock:
            if os.path.isfile(dst):  # someone finished while we waited
                return {"path": dst, "width": w, "height": h,
                        "fps": target_fps, "preprocessed": True}
            ffmpeg = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-v", "error", "-i", base_path,
                     "-vf", f"scale={w}:{h},fps={target_fps}",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                     dst],
                    capture_output=True)
            except Exception as exc:
                self.node.get_logger().error(
                    f"preprocess failed for '{media_id}': {exc}")
                return {"path": base_path, "width": w, "height": h,
                        "fps": target_fps, "preprocessed": False}
            if not os.path.isfile(dst):
                self.node.get_logger().error(
                    f"preprocess produced no file for '{media_id}'")
                return {"path": base_path, "width": w, "height": h,
                        "fps": target_fps, "preprocessed": False}
            self.node.get_logger().info(
                f"preprocessed '{media_id}' -> {w}x{h}/{target_fps:.6g} fps")
        return {"path": dst, "width": w, "height": h,
                "fps": target_fps, "preprocessed": True}


class _Player:
    """Backend-owned media cursor.

    The browser never sends pixels; it only sends controls (play/pause/stop
    at a time). This player decodes the media file inside this process and
    publishes frames plus the lane points the cursor passes as it runs its own
    wall-clock clock. Media bytes are fed to ffmpeg via stdin because the
    snap-installed ffmpeg on this box cannot open dot-prefixed ~/.ros paths.
    """

    def __init__(self, backend):
        self.b = backend
        self._lock = threading.Lock()
        self._proc = None
        self._reader_thread = None
        self._pacer_thread = None
        self._watchdog_thread = None
        self._frames = deque()
        self._cv = None
        self._alive = False
        self._playing = False
        self._t = 0.0
        self._media_id = None
        self._path = None
        self._w = 0
        self._h = 0
        self._fps = 30.0
        self._topic = "/media_player/image"
        self._frame_id = "media_player"
        self._tracks = []

    def _ffmpeg(self):
        return shutil.which("ffmpeg") or "/snap/bin/ffmpeg"

    def _stop(self):
        with self._lock:
            self._playing = False
            self._alive = False
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        for t in (self._reader_thread, self._pacer_thread,
                  self._watchdog_thread):
            if (t is not None and t.is_alive()
                    and t is not threading.current_thread()):
                t.join(timeout=2)

    def _decode_frame_at(self, path, t, width, height) -> bytes | None:
        """One-shot decode of a single frame at time t into raw RGBA.

        Uses real-file input (not a stdin pipe) so ffmpeg can fast-seek to t
        with ``-ss`` as an *input* option. Feeding the whole file via stdin
        forced ffmpeg to decode forward from the start to reach t, which
        fails partway through any stream with a damaged/truncated tail (the
        "Error marking filters as finished / received no packets" crash).
        """
        try:
            proc = subprocess.run(
                [self._ffmpeg(), "-v", "error",
                 "-ss", str(t), "-i", path,
                 "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba",
                 "-s", f"{width}x{height}", "-"],
                capture_output=True, timeout=30)
        except Exception as exc:
            self.b.node.get_logger().error(
                f"frame decode at t={t} failed: {exc}")
            return None
        need = width * height * 4
        if proc.returncode != 0 or len(proc.stdout) != need:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            lines = err.splitlines()
            detail = "\n".join(f"  {l}" for l in lines[-3:]) if lines else ""
            self.b.node.get_logger().error(
                f"frame decode at t={t} got {len(proc.stdout)}/{need} bytes "
                f"(rc={proc.returncode}){(':\n' + detail) if detail else ''}")
            return None
        return proc.stdout

    def _points_at(self, t):
        hits = []
        for tr in self._tracks:
            for p in tr.get("points") or []:
                if abs(t - p.get("t", 0)) <= 0.05:
                    hits.append((tr.get("topic"), tr.get("frameId"),
                                 tr.get("stamped", True),
                                 p.get("x", 0.5), p.get("y", 0.5)))
        return hits

    def _publish_frame(self, rgba):
        self.b.publish_image(self._topic, self._frame_id,
                             self._w, self._h, "rgba8", rgba)
        for topic, fid, stamped, x, y in self._points_at(self._t):
            self.b.publish_point(topic or self._topic, fid or self._frame_id,
                                 x, y, stamped)

    def _spawn_stream(self):
        ffmpeg = self._ffmpeg()
        # The source is a real, seekable file (already normalized to the
        # publish size/fps by offline preprocessing), so decode from the path
        # and fast-seek to the cursor with an input ``-ss``. ffmpeg decodes way
        # faster than wall-clock fps, so read_frames() throttles it via
        # back-pressure (blocking on a full buffer) to keep production locked
        # to pace()'s consumption; stdin piping is gone: a pipe has no index,
        # so ffmpeg can't seek and dies on damaged stream tails.
        args = [ffmpeg, "-v", "error",
                "-ss", str(self._t), "-i", self._path,
                "-f", "rawvideo", "-pix_fmt", "rgba",
                "-s", f"{self._w}x{self._h}", "-"]
        self._proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self._frames.clear()
        self._frame_bytes = self._w * self._h * 4
        self._alive = True
        # Reader and pacer synchronize on this condition so the reader only
        # fills the buffer up to its cap and then *blocks* (throttling ffmpeg
        # via back-pressure on the pipe) instead of racing ahead to EOF and
        # discarding frames the pacer hasn't consumed yet.
        self._cv = threading.Condition(self._lock)

        def read_frames():
            n = self._frame_bytes
            cap = int(self._fps * 2)
            while self._alive:
                try:
                    raw = self._proc.stdout.read(n)
                except Exception:
                    break
                if not raw or len(raw) != n:
                    break
                got = False
                while not got:
                    with self._cv:
                        if not self._alive:
                            break
                        if len(self._frames) < cap:
                            self._frames.append(raw)
                            got = True
                        else:
                            self._cv.wait()  # full: wait for pace() to drain
                if not got:
                    break
            with self._cv:
                self._alive = False
                self._cv.notify_all()
        self._reader_thread = threading.Thread(target=read_frames, daemon=True)
        self._reader_thread.start()

        def watchdog():
            self._reader_thread.join(timeout=int(self._fps * 2) + 2)
            with self._cv:
                empty = not self._frames and self._alive
            if empty:
                self.b.node.get_logger().error(
                    "play stream produced no frames — check the ffmpeg "
                    "codec / media file")
                with self._lock:
                    self._alive = False
        self._watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        self._watchdog_thread.start()

        def pace():
            period = 1.0 / self._fps
            while True:
                time.sleep(period)
                stop = False
                with self._cv:
                    if not self._playing:
                        return
                    self._t += period
                    rgba = self._frames.popleft() if self._frames else None
                    # EOF only ends the run once the buffered tail is drained.
                    if rgba is None and not self._alive:
                        stop = True
                    if rgba is not None:
                        self._cv.notify_all()  # made room for the reader
                if stop:
                    return
                if rgba is not None:
                    self._publish_frame(rgba)
        self._pacer_thread = threading.Thread(target=pace, daemon=True)
        self._pacer_thread.start()

    def play(self, media_id, path, t, width, height, fps,
             topic, frame_id, tracks):
        self._stop()
        with self._lock:
            self._media_id = media_id
            self._path = path
            self._t = max(0.0, float(t))
            self._w = int(width)
            self._h = int(height)
            self._fps = max(1.0, float(fps))
            self._topic = topic
            self._frame_id = frame_id
            self._tracks = tracks
        self._spawn_stream()
        with self._lock:
            self._playing = True

    def set_frame(self, t, width, height):
        """Paused update: set the cursor and publish the single frame at t."""
        self._stop()
        with self._lock:
            self._t = max(0.0, float(t))
            if width:
                self._w = int(width)
            if height:
                self._h = int(height)
        rgba = None
        if self._path:
            rgba = self._decode_frame_at(self._path, self._t, self._w, self._h)
        if rgba:
            self._publish_frame(rgba)


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
            clean.append({
                "key": key,
                "name": name,
                "color": color,
                # Persist per-track publish settings too: the frontend edits
                # topic/frameId/stamped in the track settings dialog, and
                # _points_at/_publish_frame read them back for publishing.
                "topic": (t.get("topic") or "").strip() if isinstance(t.get("topic"), str) else "",
                "frameId": (t.get("frameId") or "").strip() if isinstance(t.get("frameId"), str) else "",
                "stamped": bool(t.get("stamped", True)),
                "points": points,
            })
        out = {"tracks": clean}
        if isinstance(payload.get("topic"), str):
            out["topic"] = payload["topic"].strip()
        if isinstance(payload.get("frame_id"), str):
            out["frame_id"] = payload["frame_id"].strip()
        if isinstance(payload.get("fps"), (int, float)):
            fps = float(payload["fps"])
            out["fps"] = fps if (fps > 0 and fps == fps) else 0

        # Capture the previously-stored markers BEFORE saving, so we can detect
        # and publish any newly-added point on the spot. This makes a marker
        # dropped by clicking the video publish immediately, not only when the
        # cursor later crosses its timestamp during playback/scrubbing.
        old = self.backend.get_timeline(media_id) or {}
        old_marks = set()

        for tr in (old.get("tracks") or []) if isinstance(old.get("tracks"), list) else []:
            for p in tr.get("points") or []:
                if isinstance(p, dict):
                    old_marks.add((tr.get("key"), p.get("t"), p.get("x"), p.get("y")))
        for tr in clean:
            for p in tr.get("points") or []:
                mark = (tr["key"], p["t"], p["x"], p["y"])
                if mark in old_marks:
                    continue
                old_marks.add(mark)
                topic = tr.get("topic") or payload.get("topic") or "/media_player/image"
                frame_id = tr.get("frameId") or payload.get("frame_id") or "media_player"
                self.backend.publish_point(topic, frame_id,
                                           p["x"], p["y"], tr.get("stamped", True))
        self.backend.save_timeline(media_id, out)
        self.backend.node.get_logger().info(
            f"saved timeline for '{media_id}' ({len(clean)} tracks, "
            f"{sum(len(c['points']) for c in clean)} markers)")
        self._respond(200, {"ok": True})

    def _handle_preprocess(self) -> None:
        """Normalize a video for publishing and return the resolved stream.

        Body (JSON): { media_id, width, height, fps }.
        Blocks until the normalized stream is ready (a cache hit is fast; a
        fresh transcode takes as long as it takes), then returns the url the
        browser should play from — together with the size/fps that will be
        published. The frontend holds playback behind a loading spinner until
        this resolves, so the browser and ROS always play the same stream.
        """
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
        if not isinstance(payload, dict):
            self._respond(400, {"error": "expected JSON object"})
            return
        media_id = payload.get("media_id")
        rec = self.backend.get_media(media_id) if media_id else None
        if rec is None:
            self._respond(404, {"error": "media not found"})
            return
        path = os.path.join(self.backend.media_dir, media_id)
        if not os.path.isfile(path):
            self._respond(404, {"error": "media file missing"})
            return
        try:
            width = int(payload.get("width") or 0)
            height = int(payload.get("height") or 0)
        except (TypeError, ValueError):
            self._respond(400, {"error": "bad dimensions"})
            return
        try:
            fps = float(payload.get("fps"))
        except (TypeError, ValueError):
            fps = 0.0

        stream = self.backend.preprocess(media_id, path, width, height, fps)
        if stream.get("preprocessed"):
            url = "/media_pp/" + os.path.basename(stream["path"])
        else:
            url = "/media/" + urllib.parse.quote(media_id)
        self._respond(200, {
            "ready": True,
            "preprocessed": bool(stream.get("preprocessed")),
            "url": url,
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "fps": float(stream.get("fps") or 0),
        })

    def _serve_preprocessed(self, name: str) -> None:
        """Serve a normalized (preprocessed) stream by cache filename."""
        name = os.path.basename(urllib.parse.unquote(name))
        path = os.path.join(self.backend.preprocessed_dir, name)
        if not name or not os.path.isfile(path):
            self._respond(404, {"error": "not found"})
            return
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                start_s, _, end_s = range_hdr[6:].partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
                length = end - start + 1
                f.seek(start)
                data = f.read(length)
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(f.read())

    def _handle_control(self) -> None:
        """Accept a playback control from the browser and drive the backend
        cursor. The browser never sends pixels — only commands.

        Body (JSON):
          { media_id, cmd ("play"|"pause"|"stop"|"scrub"),
            t, width, height, fps, topic, frame_id }
        """
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
        if not isinstance(payload, dict):
            self._respond(400, {"error": "expected JSON object"})
            return
        media_id = payload.get("media_id")
        cmd = payload.get("cmd")
        if not media_id or not cmd:
            self._respond(400, {"error": "'media_id' and 'cmd' required"})
            return
        rec = self.backend.get_media(media_id)
        if rec is None:
            self._respond(404, {"error": "media not found"})
            return
        path = os.path.join(self.backend.media_dir, media_id)
        if not os.path.isfile(path):
            self._respond(404, {"error": "media file missing"})
            return
        try:
            t = float(payload.get("t", 0))
        except (TypeError, ValueError):
            t = 0.0
        width = int(payload.get("width") or 0)
        height = int(payload.get("height") or 0)
        try:
            fps = float(payload.get("fps"))
        except (TypeError, ValueError):
            fps = 30.0
        topic = payload.get("topic") or "/media_player/image"
        frame_id = payload.get("frame_id") or "media_player"

        timeline = self.backend.get_timeline(media_id) or {}
        tracks = timeline.get("tracks", [])
        if not isinstance(tracks, list):
            tracks = []
        player = self.backend._player

        # Publish from an offline-normalized copy when possible (cached fast,
        # built once on visit/settings change; only transcodes on cache miss).
        # Its native rate == publish rate, so live streaming is 1:1 and can't
        # drain. Falls back to the original file when ffmpeg is unavailable.
        stream = self.backend.preprocess(media_id, path, width, height, fps)
        s_path, s_w, s_h, s_fps = (
            stream["path"], stream["width"], stream["height"], stream["fps"])
        src_note = " (preprocessed)" if stream.get("preprocessed") else " (source)"

        self.backend.node.get_logger().info(
            f"control cmd={cmd} media_id={media_id!r} t={t:.3f} "
            f"fps={fps} target={width if width else 'natural'}x"
            f"{height if height else 'natural'} topic={topic!r} "
            f"frame_id={frame_id!r} tracks={len(tracks)} "
            f"stream={s_w if s_w else 'native'}x"
            f"{s_h if s_h else 'native'}@{s_fps:.6g}fps{src_note}")

        if cmd == "play":
            player.play(media_id, s_path, t, s_w, s_h, s_fps,
                        topic, frame_id, tracks)
            self._respond(200, {"ok": True, "cmd": "play", "t": t})
            return
        if cmd in ("pause", "stop", "scrub"):
            player.set_frame(t, s_w, s_h)
            self._respond(200, {"ok": True, "cmd": cmd, "t": t})
            return
        self._respond(400, {"error": f"unknown cmd: {cmd}"})

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
        elif route.startswith("/media_pp/"):
            self._serve_preprocessed(route[len("/media_pp/"):])
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

        if route == "/publish/control":
            self._handle_control()
            return

        if route == "/api/preprocess":
            self._handle_preprocess()
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