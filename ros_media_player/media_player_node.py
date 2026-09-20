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

import cv2
import json
import mimetypes
import os
import shutil
import math
import sqlite3
import subprocess
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
        # Skip per-frame logging while a stream is running (Play can publish
        # at 30+ fps; the hot path must not pay for log formatting each frame).
        if not self._player._playing:
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
        # Skip per-point logging during an active stream (see publish_image).
        if not self._player._playing:
            self.node.get_logger().info(
                f"published {'PointStamped' if stamped else 'Point'} "
                f"({x:.4f},{y:.4f}) on '{topic}'")

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
    """Backend-owned media cursor with the fake_input decode hot path.

    The browser never sends pixels; it only sends controls (play/pause/stop
    at a media-time). This player decodes the media file inside this process
    and publishes frames plus the lane points the cursor passes on its own
    wall-clock clock. Decoding follows the object-sensing fake_input design:
    one persistent forward ``VideoCapture`` (read() ~ms/frame; seek only when
    a request is out of order), stills decoded once and cached per file mtime,
    and playback paced by a single ROS drain timer (one frame per tick on the
    executor's spin thread) instead of ffmpeg subprocesses, a reader thread, a
    pacer thread, and a watchdog. The ffmpeg machinery still runs *off-path*
    in preprocess() to normalize a video to the publish size/fps; the hot path
    only decodes that already-prepared file.
    """

    def __init__(self, backend):
        self.b = backend
        self._lock = threading.Lock()
        self._playing = False
        self._t = 0.0
        # Whether Play repeats from the start at EOF (frontend loop button).
        self._loop = False
        # A playback action ("pause"/"stop"/"scrub") requested while the
        # cursor was still behind its target media-time. It is completed by
        # _on_drain once self._t reaches the target, instead of snapping the
        # cursor forward immediately.
        # Tuple (cmd, target_t, width, height, topic, frame_id) or None.
        self._deferred = None
        self._media_id = None
        self._path = None
        self._w = 0
        self._h = 0
        self._fps = 30.0
        self._topic = "/media_player/image"
        self._frame_id = "media_player"
        self._tracks = []
        # Persistent forward decoder (video): one open VideoCapture keyed by
        # (path, mtime), reading forward and seeking only when out of order.
        self._cap = None
        self._cap_key = None
        self._next_frame = 0
        # Cached still pixels (image): RGBA bytes keyed by (path, mtime), so a
        # still is decoded exactly once and the per-publish hot path just
        # copies the bytes into the Image message (mirrors fake_input._still_rgb).
        self._still_key = None
        self._still_bytes = None
        # Single drain timer paces Play streaming at the configured fps; created
        # once at node setup (like fake_input) and retuned on each play.
        self._timer = self.b.node.create_timer(1.0 / 30.0, self._on_drain)

    # -- decode -------------------------------------------------------------

    def _is_video_path(self) -> bool:
        return os.path.splitext(self._path)[1].lower() in VIDEO_EXTS

    def _release_decoder(self) -> None:
        if self._cap is not None:
            self._cap.release()
        self._cap = None
        self._cap_key = None
        self._next_frame = 0
        self._still_key = None
        self._still_bytes = None

    def _decode_video_frame(self, frame_no: int):
        """BGR frame of the current video at 0-based ``frame_no``, using one
        persistent VideoCapture read forward (a few ms/frame) and seeking only
        for an out-of-order request, mirroring fake_input._decode_stream. A
        changed file (mtime) reopens the capture. Called while holding
        self._lock so the ROS tick and an HTTP-triggered seek never race."""
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return None
        key = (self._path, mtime)
        if self._cap_key != key:
            if self._cap is not None:
                self._cap.release()
            self._cap = cv2.VideoCapture(self._path)
            if self._cap is None or not self._cap.isOpened():
                self._cap = None
                self._cap_key = None
                return None
            self._cap_key = key
            self._next_frame = 0
        if frame_no == self._next_frame:
            ok, bgr = self._cap.read()
            if ok:
                self._next_frame += 1
                return bgr
            # EOF mid-stream: drop the cursor and bail.
            self._cap.release()
            self._cap = None
            self._cap_key = None
            return None
        if self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no):
            ok, bgr = self._cap.read()
            if ok:
                self._next_frame = frame_no + 1
                return bgr
        self._cap.release()
        self._cap = None
        self._cap_key = None
        return None

    def _still_rgba(self) -> bytes | None:
        """Decoded RGBA bytes of the current still image, decoded once and
        cached per file mtime (mirroring fake_input._still_rgb). A still's
        pixels never change after normalization, so the file decode is off the
        per-publish hot path; the on-line cost is only the byte copy."""
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return None
        key = (self._path, mtime)
        if self._still_key == key and self._still_bytes is not None:
            return self._still_bytes
        img = cv2.imread(self._path, cv2.IMREAD_COLOR)
        if img is None:
            return None
        rgba = cv2.cvtColor(img, cv2.COLOR_BGR2RGBA)
        self._still_key = key
        self._still_bytes = rgba.tobytes()
        return self._still_bytes

    def _decode_frame(self, t: float) -> bytes | None:
        """RGBA bytes of the frame at media-time ``t`` of the current media."""
        if self._is_video_path():
            bgr = self._decode_video_frame(int(round(t * self._fps)))
            if bgr is None:
                return None
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA).tobytes()
        return self._still_rgba()

    def _points_at(self, t):
        hits = []
        for tr in self._tracks:
            for p in tr.get("points") or []:
                if abs(t - p.get("t", 0)) <= 0.05:
                    hits.append((tr.get("topic"), tr.get("frameId"),
                                 tr.get("stamped", True),
                                 p.get("x", 0.5), p.get("y", 0.5)))
        return hits

    def _publish_frame(self, rgba: bytes) -> None:
        self.b.publish_image(self._topic, self._frame_id,
                             self._w, self._h, "rgba8", rgba)
        for topic, fid, stamped, x, y in self._points_at(self._t):
            self.b.publish_point(topic or self._topic, fid or self._frame_id,
                                 x, y, stamped)

    # -- pacing: ROS drain timer, runs on the executor's spin thread ----------

    def _on_drain(self) -> None:
        """One pacing tick of playback, at 1/fps. Runs on the executor's spin
        thread, so rclpy publish calls are safe. Each tick advances the cursor
        and publishes one frame; an EOF ends the run once reached. fake_input
        drains at most one queued publish per tick; here the browser sends a
        single play command so Play emits a frame on every tick."""
        with self._lock:
            if not self._playing:
                self._release_decoder()  # drop decoder resources while idle
                return
            self._t += 1.0 / self._fps
            rgba = self._decode_frame(self._t)
            stop = rgba is None  # EOF
            if stop and self._loop:
                # Loop: rewind the cursor to the start and carry on instead of
                # ending the run. Decoding frame 0 re-opens the decoder that
                # the EOF path released. Skip publishing the duplicated EOF
                # tick if the rewind decode also fails.
                self._t = 0.0
                rgba = self._decode_frame(self._t)
                stop = rgba is None
        if rgba is not None:
            self._publish_frame(rgba)
        if stop:
            self._stop()
            return
        with self._lock:
            deferred = self._deferred
            reached = deferred is not None and self._t >= deferred[1]
            if reached:
                self._deferred = None
        if reached:
            dcmd, dt, dw, dh, dtopic, dfid = deferred
            self._apply_action(dcmd, dt, dw, dh, dtopic, dfid)

    def _stop(self) -> None:
        with self._lock:
            self._playing = False
            self._deferred = None
            self._release_decoder()

    # -- public API (called from the HTTP server threads) ----------------------

    def play(self, media_id, path, t, width, height, fps,
             topic, frame_id, tracks, loop=False):
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
            self._playing = True
            self._loop = bool(loop)
        # Retune the single drain timer to the new frame rate; the next tick
        # decodes from the start point (the first tick seeks the decoder).
        if self._timer is not None:
            self._timer.timer_period_ns = int((1.0 / self._fps) * 1e9)

    def queue_action(self, cmd, t, width, height, topic=None, frame_id=None):
        """Queue a pause/stop/scrub with deferred semantics.

        The frontend issues these relative to its own timeline. If a play
        stream is currently running and the cursor is still *behind* the
        requested media-time, keep streaming (publishing every frame) and only
        apply the action once _on_drain reaches ``t``. If the cursor is already
        at/on the far side of ``t`` -- or not streaming at all -- apply it
        immediately. This way the node acts at that point in the media, never
        by snapping forward past frames it hasn't reached yet.

        ``topic``/``frame_id`` (optional) are the configured publish settings
        to honor when the action applies. They matter most in the *cold* case
        (no play stream running, e.g. the frontend sending ``stop`` on mount):
        without them the cursor would fall back to its defaults and publish to
        the wrong topic. When omitted (None), the current settings are kept.
        """
        with self._lock:
            lagging = self._playing and self._t < t
            if lagging:
                self._deferred = (cmd, t, width, height, topic, frame_id)
                return
        self._apply_action(cmd, t, width, height, topic, frame_id)

    def _apply_action(self, cmd, t, width=None, height=None, topic=None,
                      frame_id=None):
        """Paused update for pause/stop/scrub: stop the stream and publish the
        single frame at ``t`` (with the cursor left there). The configured
        ``topic``/``frame_id`` publish settings are applied (defaulting to the
        current ones when omitted) so a cold stop/scrub that arrives before any
        play publishes to the user's configured topic, not the default."""
        self._stop()
        rgba = None
        with self._lock:
            self._t = max(0.0, float(t))
            if width:
                self._w = int(width)
            if height:
                self._h = int(height)
            if topic:
                self._topic = topic
            if frame_id:
                self._frame_id = frame_id
            if self._path:
                rgba = self._decode_frame(self._t)
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
        # Return the full envelope so the frontend can restore topic, frame_id,
        # fps, width, and height settings on reload, not just the tracks.
        self._respond(200, {
            "media": media_id,
            "tracks": data.get("tracks", []),
            "topic": data.get("topic", ""),
            "frame_id": data.get("frame_id", ""),
            "fps": data.get("fps") or 0,
            "width": data.get("width") or 0,
            "height": data.get("height") or 0,
        })

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
        # Merge onto any previously stored envelope so a POST that omits fields
        # (e.g. only tracks) doesn't wipe earlier settings; tracks always refresh.
        old = self.backend.get_timeline(media_id) or {}
        out = dict(old)
        out["tracks"] = clean
        if isinstance(payload.get("topic"), str):
            out["topic"] = payload["topic"].strip()
        if isinstance(payload.get("frame_id"), str):
            out["frame_id"] = payload["frame_id"].strip()
        # fps/width/height sanitized (reject bools and NaN/inf) so the envelope
        # never stores junk; 0 means "keep source size / native rate".
        fps = payload.get("fps")
        if isinstance(fps, (int, float)) and not isinstance(fps, bool):
            f = float(fps)
            out["fps"] = f if (f > 0 and math.isfinite(f)) else 0
        for key in ("width", "height"):
            v = payload.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                try:
                    n = int(v)
                except (TypeError, ValueError, OverflowError):
                    n = 0
                out[key] = n if (n > 0 and math.isfinite(float(v))) else 0

        # Capture the previously-stored markers BEFORE saving, so we can detect
        # and publish any newly-added point on the spot. This makes a marker
        # dropped by clicking the video publish immediately, not only when the
        # cursor later crosses its timestamp during playback/scrubbing.
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
        loop = bool(payload.get("loop", False))

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
                        topic, frame_id, tracks, loop)
            self._respond(200, {"ok": True, "cmd": "play", "t": t})
            return
        if cmd == "scrub":
            # Scrub is one-shot and never deferred, and it always pauses the
            # published stream: stop the live stream and publish the single
            # frame at t immediately (it is not resumed afterward).
            player._apply_action(cmd, t, s_w, s_h, topic, frame_id)
            self._respond(200, {"ok": True, "cmd": cmd, "t": t})
            return
        if cmd in ("pause", "stop"):
            # Deferred semantics: pause/stop take effect at media-time t.
            # If the node's cursor is still behind t (frontend raced ahead),
            # keep streaming and apply only once the cursor reaches it.
            player.queue_action(cmd, t, s_w, s_h, topic, frame_id)
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