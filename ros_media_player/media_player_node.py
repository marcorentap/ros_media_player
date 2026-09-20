#!/usr/bin/env python3

"""ROS2 media player node with an embedded web backend.

The node combines an rclpy node with a dependency-free HTTP server (Python
stdlib) so a browser frontend can trigger ROS publishes without any external
web framework.  In development the Vite dev server proxies ``/api`` and
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

Architecture
------------

The playback path is *single-owner*: exactly one thread — the ``Player``
thread — ever touches the media cursor, the decoder, the tracks, or the ROS
publishers.  The HTTP server (``ThreadingHTTPServer``) runs on however many
worker threads the stdlib spawns, but those threads never mutate playback
state directly and never call into ROS.  They only talk to a pure data
component (``MediaStore``: SQLite + files + offline transcode) and enqueue
immutable commands onto the ``Player``'s queue.

There is deliberately no rclpy executor/timer in the hot path: the ``Player``
paces itself with a plain ``time.sleep`` equal to its own ``1/fps`` period,
publishing from its own thread.  ``rclpy`` needs no spin thread to publish,
and there are no subscriptions, so the executor is removed entirely.  That
eliminates the original design's two-thread-pools (HTTP workers + a
``MultiThreadedExecutor`` spin thread) both mutating the same ``_Player``
object and depending on ad-hoc locks and a cross-thread ``_deferred`` handoff.

    MediaStore   -- pure data + cache; thread-safe by construction (SQLite
                    connections are per-call, preprocess serializes per media)
    Decoder      -- pulls a single frame out of the current backing file (one
                    cv2.VideoCapture or the still cache); owns all decode
                    plumbing so Player's pacing loop stays about policy only
    commands     -- Play / Action / Point / Shutdown: immutable, named messages
                    queued to the Player instead of positional tuples, so the
                    vocabulary is easy to extend
    Publisher    -- lazy rclpy image/point publishers; only the Player thread
                    calls it, so it needs no locking
    Player       -- a daemon thread that owns the cursor/tracks/Decoder and
                    every publish; consumes named commands off a FIFO queue
    Backend      -- owns the node, wires the above together, and runs the
                    ThreadingHTTPServer that drives the whole process
    _Handler     -- HTTP routing; the only translation layer between HTTP and
                    the command queue
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import urllib.parse
from dataclasses import dataclass, field
from contextlib import closing
from datetime import datetime, timezone
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Point, PointStamped
from sensor_msgs.msg import Image as ImageMsg
from std_msgs.msg import String

# Media types the gallery accepts for upload.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".ogg"}
ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS


# ---------------------------------------------------------------------------
# MediaStore: pure data + cache. No ROS, no playback state.
# Calls are safe from any thread: each SQLite operation opens its own
# connection, and preprocess serializes the transcode of a single media id.
# ---------------------------------------------------------------------------

class MediaStore:
    """Owns the media directory, its SQLite catalog/timelines, and the
    offline preprocess cache. Thread-safe by construction."""

    def __init__(self, data_dir: str, logger=None):
        self.data_dir = data_dir
        self.media_dir = os.path.join(data_dir, "media")
        # Offline preprocessed (normalized) copies, one H.264 stream per
        # (media, width, height, fps) cache key. Publishing decodes from these
        # so the live path never re-encodes, and its native frame rate matches
        # the publish rate 1:1 (see Player).
        self.preprocessed_dir = os.path.join(data_dir, "preprocessed")
        os.makedirs(self.media_dir, exist_ok=True)
        os.makedirs(self.preprocessed_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "media.db")
        self._logger = logger
        self._pp_locks: dict[str, threading.Lock] = {}
        self._pp_locks_guard = threading.Lock()
        self._init_db()

    # -- logging helper -----------------------------------------------------

    def _log(self, level: str, msg: str) -> None:
        if self._logger is None:
            return
        # rclpy pins each call site to one severity, so route each level
        # through its own explicit call instead of a dynamic ``getattr``
        # (which crashes with 'Logger severity cannot be changed').
        if level == "error":
            self._logger.error(msg)
        elif level == "warning":
            self._logger.warning(msg)
        elif level == "debug":
            self._logger.debug(msg)
        else:
            self._logger.info(msg)

    # -- paths ---------------------------------------------------------------

    def path(self, mid: str) -> str:
        """Filesystem path of a media file (by its UUID id)."""
        return os.path.join(self.media_dir, mid)

    def preprocessed(self, name: str) -> str:
        """Filesystem path of a preprocessed stream (by cache filename)."""
        return os.path.join(self.preprocessed_dir, name)

    # -- SQLite --------------------------------------------------------------

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
            os.remove(self.path(mid))
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

    # -- offline transcode ----------------------------------------------------

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

    def _source_reusable(self, path: str) -> bool:
        """Whether the OpenCV hot path can decode ``path`` directly, so the
        player can publish from the source instead of a transcode.

        This is exactly the capability the player's Decoder needs: a build
        without an AV1/H.265/... decoder opens the file but can't produce a
        frame (fails at get-pixel-format), and no file-name or MIME check can
        predict that. So we ask OpenCV itself -- grab() pulls one frame's
        headers/codes without converting pixels, which is both cheaper than a
        full decode and sufficient to catch the codec failures. Returns False
        when the file is missing, unopened, or the first frame can't be had.
        """
        try:
            cap = cv2.VideoCapture(path)
        except Exception:
            return False
        if cap is None:
            return False
        try:
            ok = cap.isOpened() and cap.grab()
        except Exception:
            ok = False
        finally:
            cap.release()
        return bool(ok)

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
            self._log("warning",
                      f"could not probe '{media_id}' for preprocessing")
            return {"path": base_path, "width": int(width), "height": int(height),
                    "fps": float(fps), "preprocessed": False}
        w = int(width or natural_w)
        h = int(height or natural_h)
        target_fps = max(1.0, float(fps)) if (fps and fps > 0) else native_fps
        # Nothing to change: publishing from the original at native size/rate
        # is already 1:1, so skip the pointless duplicate transcode IF the hot
        # path can actually decode the source. A file whose codec OpenCV can't
        # serve (e.g. AV1 on a build with no AV1 decoder) must still be
        # transcoded, because reusing it would make the player publish nothing.
        if (w == natural_w and h == natural_h
                and abs(target_fps - native_fps) < 1e-6):
            if self._source_reusable(base_path):
                return {"path": base_path, "width": w, "height": h,
                        "fps": native_fps, "preprocessed": False}
            self._log("info",
                f"source '{media_id}' not OpenCV-decodable; "
                f"transcoding to H.264 at {w}x{h}/{target_fps:.6g} fps")

        key = f"{media_id}__{w}x{h}_{target_fps:.6g}.mp4"
        dst = self.preprocessed(key)
        if os.path.isfile(dst):
            return {"path": dst, "width": w, "height": h,
                    "fps": target_fps, "preprocessed": True}

        # Serialize builds per media so a background visit-trigger and a play
        # racing in don't transcode+overwrite the same cache file together.
        with self._pp_locks_guard:
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
                self._log("error",
                          f"preprocess failed for '{media_id}': {exc}")
                return {"path": base_path, "width": w, "height": h,
                        "fps": target_fps, "preprocessed": False}
            if not os.path.isfile(dst):
                self._log("error",
                          f"preprocess produced no file for '{media_id}'")
                return {"path": base_path, "width": w, "height": h,
                        "fps": target_fps, "preprocessed": False}
            self._log("info",
                      f"preprocessed '{media_id}' -> {w}x{h}/{target_fps:.6g} fps")
        return {"path": dst, "width": w, "height": h,
                "fps": target_fps, "preprocessed": True}


# ---------------------------------------------------------------------------
# Publisher: lazy rclpy publishers for frames and lane points.
# The Player thread is the only caller, so no locking is needed and none is
# taken -- the guarantee is structural, not enforced at runtime.
# ---------------------------------------------------------------------------

class Publisher:
    """Publishes sensor_msgs/Image frames and geometry_msgs point messages.

    Only ever called from the Player thread. Lazily creates one publisher per
    topic so a configurable topic doesn't require a fixed set up front.
    Per-frame/point logging is deliberately omitted: frames publish at fps
    (several per second) and a per-message log line would just spam."""

    def __init__(self, node: Node):
        self.node = node
        self._img_pubs: dict[str, object] = {}
        self._stamped_pubs: dict[str, object] = {}
        self._plain_pubs: dict[str, object] = {}

    def publish_image(self, topic: str, frame_id: str,
                      width: int, height: int, encoding: str, data: bytes) -> None:
        pub = self._img_pubs.get(topic)
        if pub is None:
            pub = self.node.create_publisher(ImageMsg, topic, 10)
            self._img_pubs[topic] = pub
        msg = ImageMsg()
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.header.frame_id = frame_id
        msg.height = int(height)
        msg.width = int(width)
        msg.encoding = encoding
        msg.is_bigendian = False
        msg.step = int(width) * (4 if encoding == "rgba8" else 3)
        msg.data = data
        pub.publish(msg)

    def publish_point(self, topic: str, frame_id: str,
                      x: float, y: float, stamped: bool) -> None:
        """Publish a lane-track point as PointStamped (or bare Point)."""
        now = self.node.get_clock().now().to_msg()
        if stamped:
            pub = self._stamped_pubs.get(topic)
            if pub is None:
                pub = self.node.create_publisher(PointStamped, topic, 10)
                self._stamped_pubs[topic] = pub
            msg = PointStamped()
            msg.header.stamp = now
            msg.header.frame_id = frame_id
            msg.point.x = float(x)
            msg.point.y = float(y)
            msg.point.z = 0.0
            pub.publish(msg)
        else:
            pub = self._plain_pubs.get(topic)
            if pub is None:
                pub = self.node.create_publisher(Point, topic, 10)
                self._plain_pubs[topic] = pub
            msg = Point()
            msg.x = float(x)
            msg.y = float(y)
            msg.z = 0.0
            pub.publish(msg)


# ---------------------------------------------------------------------------
# Player: the single owner of the playback cursor. A daemon thread consuming
# a FIFO command queue; it also paces Play streaming with a plain sleep. By
# construction only this thread decodes, advances the cursor, or publishes.
# ---------------------------------------------------------------------------

class Decoder:
    """Decode a single frame from the current backing file on the Player
    thread. Owns the persistent ``cv2.VideoCapture`` and the still-image cache,
    so the pacing loop never touches decoder plumbing directly.

    ``decode(t, fps)`` returns flattened RGBA bytes for the frame at media-time
    ``t``, or ``None`` when the frame cannot be produced. A file that changed
    on disk (mtime) is reopened lazily; reading forward is sequential and a
    seek repositions the capture. A transient read/seek failure on a frame
    short of the known end is retried once from a clean reopen, then reported
    as ``None`` so the Player can treat it as the end of the stream instead of
    stalling."""

    def __init__(self, logger, path: str, is_video: bool,
                 width: int, height: int):
        self._log = logger
        self.path = path
        self.is_video = bool(is_video)   # from the media's MIME, not the file ext
        self.width = int(width)
        self.height = int(height)
        self.frame_count = 0             # known frames of the open capture (0 = ?)
        self._cap = None                 # persistent forward VideoCapture
        self._cap_key = None             # (path, mtime) this capture corresponds to
        self._next_frame = 0             # next 0-based frame a forward read yields
        self._still_key = None
        self._still_bytes = None

    def release(self) -> None:
        """Drop the capture and caches; the next decode reopens from scratch."""
        if self._cap is not None:
            self._cap.release()
        self._cap = None
        self._cap_key = None
        self._next_frame = 0
        self.frame_count = 0
        self._still_key = None
        self._still_bytes = None

    # -- helpers (called only from the Player thread) --------------------------

    def _open_video(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        key = (self.path, mtime)
        if self._cap_key == key:
            return
        if self._cap is not None:
            self._cap.release()
        cap = cv2.VideoCapture(self.path)
        if cap is None or not cap.isOpened():
            self._cap = None
            self._cap_key = None
            self.frame_count = 0
            return
        self._cap = cap
        self._cap_key = key
        self._next_frame = 0
        try:
            self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        except (TypeError, ValueError):
            self.frame_count = 0
        self._log.info(f"decoder opened: frame count={self.frame_count}")

    def _read_frame(self, frame_no: int):
        """BGR frame at 0-based ``frame_no``, or None on failure/EOF."""
        if self._cap is None:
            self._open_video()
        if self._cap is None:
            return None
        if self.frame_count and frame_no >= self.frame_count:
            # Past the known end of the file: genuine EOF, not a seek failure.
            return None
        if frame_no == self._next_frame:
            # In sequence: one cheap forward read.
            ok, bgr = self._cap.read()
            if ok:
                self._next_frame += 1
                return bgr
            return None
        if self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no):
            ok, bgr = self._cap.read()
            if ok:
                self._next_frame = frame_no + 1
                return bgr
        return None

    def decode(self, t: float, fps: float) -> bytes | None:
        """RGBA bytes of the frame at media-time ``t``, or None when the frame
        can't be produced (EOF, or a codec the platform can't serve)."""
        if not self.is_video:
            return self._still_rgba()
        frame_no = int(round(t * fps))
        if self.frame_count and frame_no >= self.frame_count:
            return None
        rgba = self._rgba_frame(frame_no)
        if rgba is None:
            # A seek or read can fail transiently (or the platform's codec
            # can't serve this frame). Reopen from a clean state and try once
            # more before giving up -- enough to stop a one-off hiccup from
            # freezing the stream, without the old multi-retry warning storm.
            self.release()
            rgba = self._rgba_frame(frame_no)
        return rgba

    def _rgba_frame(self, frame_no: int) -> bytes | None:
        bgr = self._read_frame(frame_no)
        if bgr is None:
            return None
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA).tobytes()

    def _still_rgba(self) -> bytes | None:
        """RGBA bytes of the still image, cached per file mtime (the per-publish
        hot path only copies the cached bytes)."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        key = (self.path, mtime)
        if self._still_key == key and self._still_bytes is not None:
            return self._still_bytes
        img = cv2.imread(self.path, cv2.IMREAD_COLOR)
        if img is None:
            return None
        rgba = cv2.cvtColor(img, cv2.COLOR_BGR2RGBA)
        self._still_key = key
        self._still_bytes = rgba.tobytes()
        return self._still_bytes


# -- playback commands: immutable, named values instead of positional tuples so
#    the HTTP layer and the Player speak a vocabulary that's easy to extend. --

@dataclass
class Play:
    media_id: str
    path: str
    t: float
    width: int
    height: int
    fps: float
    topic: str
    frame_id: str
    tracks: list = field(default_factory=list)
    loop: bool = False
    is_video: bool = False


@dataclass
class Action:
    cmd: str                  # "pause" | "stop" | "scrub"
    t: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    topic: str | None = None
    frame_id: str | None = None
    is_video: bool | None = None


@dataclass
class Point:
    topic: str
    frame_id: str
    x: float
    y: float
    stamped: bool


@dataclass
class Shutdown:
    pass


# ---------------------------------------------------------------------------
# Player: the single owner of playback. A daemon thread -- the only thread
# that touches the cursor, the decoder, the tracks, or the publishers. It
# consumes immutable commands off a FIFO queue and paces Play with a plain
# sleep, so there is no rclpy executor and nothing to race. Decoding lives in
# the Decoder class; this class owns the "what is happening" state and the
# pacing policy on top of it, and it is the only place that publishes to ROS.
# ---------------------------------------------------------------------------

class Player(threading.Thread):
    def __init__(self, publisher: Publisher, logger=None):
        super().__init__(daemon=True, name="media-player")
        self._publisher = publisher
        self._logger = logger
        self._q: "queue.Queue[object]" = queue.Queue()
        self._stop_event = threading.Event()
        # --- playback state: written only from this thread ---
        self._decoder: Decoder | None = None
        self._t = 0.0
        self._playing = False
        self._fps = 30.0
        self._w = 0
        self._h = 0
        self._topic = "/media_player/image"
        self._frame_id = "media_player"
        self._tracks: list = []
        self._loop = False
        # Media-time before which markers have already been published this
        # playback pass. The decoded cursor is frame-quantized, but playback
        # still flows continuously though every real time in between, so
        # markers are fired on the interval (start, current] reached by this
        # variable rather than rounded to the nearest frame.
        self._marker_span_start = 0.0
        # Deferred pause/stop actions waiting for the cursor to reach their
        # media-time while a running stream is still behind it.
        self._pending: list[Action] = []

    def _log(self, level: str, msg: str) -> None:
        if self._logger is None:
            return
        # rclpy pins each call site to one severity; route each level through
        # its own explicit call rather than a dynamic ``getattr`` (which would
        # crash with 'Logger severity cannot be changed between calls').
        if level == "error":
            self._logger.error(msg)
        elif level == "warning":
            self._logger.warning(msg)
        else:
            self._logger.info(msg)

    # -- public, thread-safe command API (enqueue only) -----------------------

    def play(self, media_id, path, t, width, height, fps,
             topic, frame_id, tracks=None, loop=False, is_video=False) -> None:
        self._q.put(Play(media_id, path, float(t), int(width), int(height),
                         float(fps), topic, frame_id, tracks or [], bool(loop),
                         bool(is_video)))

    def action(self, cmd, t, width, height, fps=0.0, topic=None,
               frame_id=None, is_video=None) -> None:
        """Queue a scrub/pause/stop acting at media-time ``t`` (see dispatch)."""
        self._q.put(Action(cmd, float(t), int(width or 0), int(height or 0),
                           max(0.0, float(fps)), topic, frame_id, is_video))

    def publish_point(self, topic, frame_id, x, y, stamped) -> None:
        """Publish a single lane-track point (used by timeline marker saves)."""
        self._q.put(Point(topic, frame_id, float(x), float(y), bool(stamped)))

    def shutdown(self) -> None:
        self._q.put(Shutdown())
        self._stop_event.set()

    # -- command execution (player thread only) --------------------------------

    def _dispatch(self, cmd) -> None:
        if isinstance(cmd, Play):
            self._end_stream()
            self._decoder = Decoder(self._logger, cmd.path, cmd.is_video,
                                    cmd.width, cmd.height)
            self._marker_span_start = max(0.0, float(cmd.t))
            self._t = max(0.0, cmd.t)
            self._w = int(cmd.width)
            self._h = int(cmd.height)
            self._fps = max(1.0, float(cmd.fps))
            self._topic = cmd.topic
            self._frame_id = cmd.frame_id
            self._tracks = cmd.tracks
            self._loop = bool(cmd.loop)
            self._playing = True
            self._log("info",
                f"PLAY start: t={self._t:.3f}s fps={self._fps:.1f} "
                f"{self._w}x{self._h} topic={self._topic!r} "
                f"tracks={len(self._tracks)} loop={self._loop} "
                f"file={os.path.basename(cmd.path)}")
        elif isinstance(cmd, Action):
            if cmd.is_video is not None and self._decoder is not None:
                self._decoder.is_video = bool(cmd.is_video)
            if cmd.cmd == "scrub" or not self._playing or self._t >= cmd.t:
                self._log("info",
                    f"{cmd.cmd.upper()} APPLY now at t={cmd.t:.3f}s "
                    f"(cursor={self._t:.3f}s)")
                self._apply_action(cmd)
            else:
                self._pending.append(cmd)
                self._pending.sort(key=lambda a: a.t)
                self._log("info",
                    f"{cmd.cmd.upper()} DEFERRED: cursor {self._t:.3f}s still "
                    f"behind t={cmd.t:.3f}s; applying when reached")
        elif isinstance(cmd, Point):
            self._publisher.publish_point(
                cmd.topic, cmd.frame_id, cmd.x, cmd.y, cmd.stamped)
        elif isinstance(cmd, Shutdown):
            pass

    # -- decode + publish helpers (player thread only) --------------------------

    def _decode(self, t: float) -> bytes | None:
        """RGBA bytes of the frame at ``t``, or None when it can't be decoded."""
        if self._decoder is None:
            return None
        return self._decoder.decode(t, self._fps)

    def _fire_markers_between(self, start, end):
        """Publish every marker whose media-time falls in ``(start, end]``.

        The interval advances continuously as playback flows (each tick covers
        exactly the span the cursor crossed), so a marker placed anywhere
        between two frames still fires on the tick that crosses it — no
        frame-quantization of the marker time."""
        for tr in self._tracks:
            for p in tr.get("points") or []:
                pt = p.get("t", 0)
                if not (start < pt <= end):
                    continue
                self._publisher.publish_point(
                    tr.get("topic") or self._topic,
                    tr.get("frameId") or self._frame_id,
                    p.get("x", 0.5), p.get("y", 0.5),
                    tr.get("stamped", True))

    def _fire_markers_at(self, t):
        """Publish markers tied to a discrete pause/stop/scrub landing. A
        jumped-to media-time rarely lands exactly on a marker's stored
        timestamp, so accept any marker within half a frame of ``t``."""
        tol = (0.5 / self._fps) if self._fps else 0.05
        for tr in self._tracks:
            for p in tr.get("points") or []:
                pt = p.get("t", 0)
                if abs(pt - t) > tol:
                    continue
                self._publisher.publish_point(
                    tr.get("topic") or self._topic,
                    tr.get("frameId") or self._frame_id,
                    p.get("x", 0.5), p.get("y", 0.5),
                    tr.get("stamped", True))

    def _publish_image(self, rgba: bytes) -> None:
        self._publisher.publish_image(
            self._topic, self._frame_id, self._w, self._h, "rgba8", rgba)

    # -- stream lifecycle (player thread only) ---------------------------------

    def _end_stream(self) -> None:
        self._log("info", f"stream END (t={self._t:.3f}s)")
        self._playing = False
        self._pending = []
        # The decoder is kept: pause/stop/scrub re-decode the current frame
        # from it, and a later Play replaces it with a fresh one.

    def _apply_action(self, act: Action) -> None:
        """Paused update for pause/stop/scrub: stop the stream and publish the
        single frame at ``act.t`` (leaving the cursor there), honoring the
        configured topic/frame_id publish settings."""
        self._end_stream()
        self._t = max(0.0, float(act.t))
        if act.width:
            self._w = int(act.width)
        if act.height:
            self._h = int(act.height)
        if act.fps:
            self._fps = max(1.0, float(act.fps))
        if act.topic:
            self._topic = act.topic
        if act.frame_id:
            self._frame_id = act.frame_id
        rgba = self._decode(self._t) if self._decoder is not None else None
        if rgba:
            self._publish_image(rgba)
            self._fire_markers_at(self._t)
        # Restart marker firing from the landing point, so playback resumed
        # after this only fires markers whose time lies ahead of the new cursor.
        self._marker_span_start = self._t
        self._log("info",
            f"{act.cmd.upper()} done: cursor at t={self._t:.3f}s "
            f"published={'yes' if rgba else 'no'}")

    # -- the pacing loop -------------------------------------------------------

    def _at_eof(self, t: float) -> bool:
        return bool(self._decoder is not None and self._decoder.frame_count
                    and int(round(t * self._fps)) >= self._decoder.frame_count)

    def _drain_commands(self) -> bool:
        """Run every queued command now (in FIFO order). Returns True if any."""
        got = False
        while True:
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                break
            got = True
            self._dispatch(cmd)
            if self._stop_event.is_set():
                break
        return got

    def _advance(self) -> None:
        """One pacing tick of playback: advance the cursor and publish a frame,
        wrapping for loop-EOF and applying any deferred pause/stop the cursor
        has now reached."""
        span_start = self._marker_span_start
        self._t += 1.0 / self._fps
        rgba = self._decode(self._t)
        if rgba is None:
            at_eof = self._at_eof(self._t)
            if at_eof and self._loop:
                # EOF with looping on: wrap around and continue from the top.
                self._t = 0.0
                rgba = self._decode(self._t)
                # Fresh pass: markers fire again from the top of the file.
                span_start = 0.0
            if rgba is None:
                self._log("info",
                    f"stream reached end at t={self._t:.3f}s "
                    f"({'EOF' if at_eof else 'decode failed'}); stopping")
                self._end_stream()
                return
        self._publish_image(rgba)
        # Fire every marker the continuously-flowing playhead crossed (or
        # re-crossed after an EOF wrap) since the last tick.
        self._fire_markers_between(span_start, self._t)
        self._marker_span_start = self._t
        while self._pending and self._t >= self._pending[0].t:
            act = self._pending.pop(0)
            self._apply_action(act)
            if not self._playing:
                break

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._drain_commands()
                if self._stop_event.is_set():
                    break
                if self._playing:
                    self._advance()
                    # Pace to roughly one frame per the publish fps (the decode
                    # already took some time, so the real rate is <= fps).
                    time.sleep(1.0 / self._fps)
                else:
                    # Idle: wake up occasionally to re-check the queue/shutdown.
                    self._stop_event.wait(0.05)
            except Exception as exc:
                # A fault on one tick must not kill the playback thread (this
                # node's only publisher of frames) and freeze the stream inside
                # a running session. Log it, stop that stream, and keep the
                # loop alive so the next control can recover.
                self._end_stream()
                self._log("error", f"playback error: {exc!r}")
                time.sleep(0.05)


class _Handler(BaseHTTPRequestHandler):
    """Minimal JSON + media HTTP handler. Translates HTTP requests into
    MediaStore calls and Player commands; never touches playback state or ROS
    directly."""

    def __init__(self, backend, *args, **kwargs):
        self.b = backend
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # keep stderr quiet
        pass

    # -- helpers -----------------------------------------------------------

    def _respond(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        """Read a JSON request body into a dict, or None on any parse error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _serve_web(self, path: str, head_only: bool = False) -> None:
        """Serve the built frontend as an SPA from the resolved web root.

        ``path`` is the URL path (e.g. "/", "/index.html", "/assets/...").
        Unknown / client-side routes fall back to index.html so deep links and
        refreshes work without a separate static server.
        """
        if not self.b.web_dir:
            if path in ("", "/"):
                self._serve_web_hint(head_only)
            else:
                self._respond(404, {"error": "web frontend not built"})
            return
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(self.b.web_dir, rel))
        # Guard against path traversal outside the web root.
        if not (full == self.b.web_dir
                or full.startswith(self.b.web_dir + os.sep)):
            self._respond(404, {"error": "not found"})
            return
        if not os.path.isfile(full):
            # SPA fallback: anything that isn't a real file or an API route
            # serves index.html.
            full = os.path.join(self.b.web_dir, "index.html")
        if not os.path.isfile(full):
            self._respond(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self._serve_file(full, ctype, head_only)

    def _serve_web_hint(self, head_only: bool = False) -> None:
        """Serve a short HTML page explaining how to build the frontend."""
        body = ("<!doctype html><meta charset=utf-8><title>ros_media_player</title>"
                "<body style='font-family:system-ui;max-width:52ch;margin:4rem auto;"
                "line-height:1.5'><h1>ros_media_player</h1>"
                "<p>The JSON/API backend is running, but no frontend bundle was found.</p>"
                "<p>Build it with:</p>"
                "<pre>cd frontend &amp;&amp; pnpm install &amp;&amp; pnpm build</pre>"
                "<p>then restart the node (or set <code>MEDIA_PLAYER_WEB</code> to a "
                "directory containing <code>index.html</code>).</p></body>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _serve_file(self, path: str, ctype: str, head_only: bool = False) -> None:
        """Serve a file with Range support (206 partials for video seeking)."""
        if not path or not os.path.isfile(path):
            self._respond(404, {"error": "not found"})
            return
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
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if not head_only:
                    self.wfile.write(f.read())

    def _serve_media(self, mid: str, head_only: bool = False) -> None:
        mid = urllib.parse.unquote(mid)
        rec = self.b.store.get_media(mid)
        if rec is None:
            self._respond(404, {"error": "not found"})
            return
        ctype = rec["mime"] or "application/octet-stream"
        self._serve_file(self.b.store.path(mid), ctype, head_only)

    # -- media CRUD -----------------------------------------------------------

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
            with open(self.b.store.path(mid), "wb") as f:
                f.write(data)
            display = self.b.store.unique_name(os.path.basename(filename))
            mime = mimetypes.guess_type(display)[0] or "application/octet-stream"
            self.b.store.add_media(mid, display, mime, len(data))
            saved.append({"id": mid, "name": display})
            self.b.node.get_logger().info(
                f"saved media '{display}' ({mid}, {len(data)} bytes)")

        self._respond(201, {"saved": saved, "rejected": rejected})

    def _handle_rename(self) -> None:
        payload = self._read_json()
        if payload is None:
            self._respond(400, {"error": "expected JSON body"})
            return
        mid = payload.get("id")
        new_name = payload.get("newName")
        if not mid or not new_name:
            self._respond(400, {"error": "'id' and 'newName' required"})
            return
        if self.b.store.get_media(mid) is None:
            self._respond(404, {"error": "not found"})
            return
        new = os.path.basename(urllib.parse.unquote(new_name)).strip()
        if new in ("", ".", ".."):
            self._respond(400, {"error": "invalid name"})
            return
        if self.b.store.name_exists(new):
            self._respond(409, {"error": "a file with that name already exists"})
            return
        try:
            self.b.store.rename_media(mid, new)
        except sqlite3.IntegrityError:
            self._respond(409, {"error": "a file with that name already exists"})
            return
        self.b.node.get_logger().info(f"renamed media '{mid}' -> '{new}'")
        self._respond(200, {"id": mid, "name": new})

    def _handle_delete(self, mid: str) -> None:
        mid = urllib.parse.unquote(mid)
        if self.b.store.get_media(mid) is None:
            self._respond(404, {"error": "not found"})
            return
        self.b.store.delete_media(mid)
        self.b.node.get_logger().info(f"deleted media '{mid}'")
        self._respond(200, {"ok": True})

    # -- timeline ---------------------------------------------------------------

    def _handle_timeline_get(self, media_id: str) -> None:
        if self.b.store.get_media(media_id) is None:
            self._respond(404, {"error": "not found"})
            return
        data = self.b.store.get_timeline(media_id) or {}
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
        if self.b.store.get_media(media_id) is None:
            self._respond(404, {"error": "not found"})
            return
        payload = self._read_json()
        if payload is None:
            self._respond(400, {"error": "expected JSON body"})
            return
        tracks = payload.get("tracks")
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
                # topic/frameId/stamped in the track settings dialog, and the
                # Player reads them back for publishing.
                "topic": (t.get("topic") or "").strip() if isinstance(t.get("topic"), str) else "",
                "frameId": (t.get("frameId") or "").strip() if isinstance(t.get("frameId"), str) else "",
                "stamped": bool(t.get("stamped", True)),
                "points": points,
            })

        # Merge onto any previously stored envelope so a POST that omits fields
        # (e.g. only tracks) doesn't wipe earlier settings; tracks refresh.
        old = self.b.store.get_timeline(media_id) or {}
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

        # Publish any newly-added marker on the spot (through the Player, so
        # ROS publishes still originate from the single player thread). A
        # marker dropped by clicking the video therefore publishes immediately,
        # not only when the cursor later crosses its timestamp.
        prev_marks = set()
        old_tracks = old.get("tracks") if isinstance(old.get("tracks"), list) else []
        for tr in old_tracks:
            for p in tr.get("points") or []:
                if isinstance(p, dict):
                    prev_marks.add((tr.get("key"), p.get("t"), p.get("x"), p.get("y")))
        for tr in clean:
            for p in tr.get("points") or []:
                mark = (tr["key"], p["t"], p["x"], p["y"])
                if mark in prev_marks:
                    continue
                prev_marks.add(mark)
                topic = tr.get("topic") or payload.get("topic") or "/media_player/image"
                frame_id = tr.get("frameId") or payload.get("frame_id") or "media_player"
                self.b.player.publish_point(topic, frame_id,
                                            p["x"], p["y"], tr.get("stamped", True))

        self.b.store.save_timeline(media_id, out)
        self.b.node.get_logger().info(
            f"saved timeline for '{media_id}' ({len(clean)} tracks, "
            f"{sum(len(c['points']) for c in clean)} markers)")
        self._respond(200, {"ok": True})

    # -- preprocess --------------------------------------------------------------

    def _handle_preprocess(self) -> None:
        """Normalize a video for publishing and return the resolved stream.

        Body (JSON): { media_id, width, height, fps }. Blocks until the stream
        is ready (a cache hit is fast; a fresh transcode takes as long as it
        takes), then returns the url the browser should play from -- together
        with the size/fps that will be published.
        """
        payload = self._read_json()
        if payload is None:
            self._respond(400, {"error": "expected JSON object"})
            return
        media_id = payload.get("media_id")
        rec = self.b.store.get_media(media_id) if media_id else None
        if rec is None:
            self._respond(404, {"error": "media not found"})
            return
        path = self.b.store.path(media_id)
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

        stream = self.b.store.preprocess(media_id, path, width, height, fps)
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

    # -- playback control ----------------------------------------------------------

    def _handle_control(self) -> None:
        """Accept a playback control from the browser and drive the Player.

        The browser never sends pixels -- only commands. Body (JSON):
        { media_id, cmd ("play"|"pause"|"stop"|"scrub"), t, width, height,
          fps, topic, frame_id }. The command is handed to the Player via its
        command queue; the response only acknowledges receipt.
        """
        payload = self._read_json()
        if payload is None:
            self._respond(400, {"error": "expected JSON object"})
            return
        media_id = payload.get("media_id")
        cmd = payload.get("cmd")
        if not media_id or not cmd:
            self._respond(400, {"error": "'media_id' and 'cmd' required"})
            return
        if self.b.store.get_media(media_id) is None:
            self._respond(404, {"error": "media not found"})
            return
        rec = self.b.store.get_media(media_id)
        # The backing file is an extensionless UUID, so the media kind comes
        # from the recorded MIME type, not the file name.
        is_video = (rec["mime"] or "").lower().startswith("video/")
        path = self.b.store.path(media_id)
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

        timeline = self.b.store.get_timeline(media_id) or {}
        tracks = timeline.get("tracks", [])
        if not isinstance(tracks, list):
            tracks = []

        # Publish from an offline-normalized copy when possible (cached fast,
        # built once on visit/settings change; only transcodes on cache miss).
        # Its native rate == publish rate, so live streaming is 1:1 and can't
        # drain. Falls back to the original file when ffmpeg is unavailable.
        stream = self.b.store.preprocess(media_id, path, width, height, fps)
        s_path, s_w, s_h, s_fps = (
            stream["path"], stream["width"], stream["height"], stream["fps"])
        src_note = " (preprocessed)" if stream.get("preprocessed") else " (source)"

        player = self.b.player
        if cmd == "play":
            player.play(media_id, s_path, t, s_w, s_h, s_fps,
                        topic, frame_id, tracks, loop, is_video=is_video)
        elif cmd == "scrub":
            player.action("scrub", t, s_w, s_h, s_fps, topic,
                          frame_id, is_video=is_video)
        elif cmd in ("pause", "stop"):
            player.action(cmd, t, s_w, s_h, s_fps, topic,
                          frame_id, is_video=is_video)
        else:
            self._respond(400, {"error": f"unknown cmd: {cmd}"})
            return

        self.b.node.get_logger().info(
            f"control cmd={cmd} media_id={media_id!r} t={t:.3f} "
            f"fps={fps} target={width if width else 'natural'}x"
            f"{height if height else 'natural'} topic={topic!r} "
            f"frame_id={frame_id!r} tracks={len(tracks)} "
            f"stream={s_w if s_w else 'native'}x"
            f"{s_h if s_h else 'native'}@{s_fps:.6g}fps{src_note}")
        self._respond(200, {"ok": True, "cmd": cmd, "t": t})

    # -- legacy command topic ---------------------------------------------------------

    def _handle_command_topic(self) -> None:
        """Legacy /publish endpoint: send a String on the command topic."""
        topic = self.b.command_topic
        data = "button-pressed"
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                payload = json.loads(self.rfile.read(length))
                if isinstance(payload, dict):
                    topic = payload.get("topic", topic)
                    data = payload.get("data", data)
        except (ValueError, json.JSONDecodeError):
            pass
        msg = String()
        msg.data = data
        self.b.pub.publish(msg)
        self.b.node.get_logger().info(f"published to '{topic}': {data!r}")
        self._respond(200, {"published": True, "topic": topic, "data": data})

    # -- routes ----------------------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        if route == "/api/health":
            self._respond(200, {"status": "ok",
                                "command_topic": self.b.command_topic})
        elif route == "/api/config":
            self._respond(200, {
                "host": self.b.host,
                "port": self.b.port,
                "command_topic": self.b.command_topic,
                "data_dir": self.b.data_dir,
            })
        elif route == "/api/media":
            self._respond(200, {"media": self.b.store.list_media()})
        elif route.startswith("/api/timeline/"):
            self._handle_timeline_get(urllib.parse.unquote(route[len("/api/timeline/"):]))
        elif route.startswith("/media_pp/"):
            self._serve_file(
                self.b.store.preprocessed(urllib.parse.unquote(route[len("/media_pp/"):])),
                "video/mp4")
        elif route.startswith("/media/"):
            self._serve_media(route[len("/media/"):])
        else:
            self._serve_web(route)

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        if route.startswith("/media/"):
            self._serve_media(route[len("/media/"):], head_only=True)
        else:
            self._serve_web(route, head_only=True)

    def do_DELETE(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/api/media/"):
            self._handle_delete(parsed.path[len("/api/media/"):])
        else:
            self._respond(405, {"error": "method not allowed"})

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        if route == "/api/media":
            self._handle_upload()
        elif route == "/api/media/rename":
            self._handle_rename()
        elif route.startswith("/api/timeline/"):
            self._handle_timeline_post(urllib.parse.unquote(route[len("/api/timeline/"):]))
        elif route == "/publish/control":
            self._handle_control()
        elif route == "/api/preprocess":
            self._handle_preprocess()
        elif route == "/publish":
            self._handle_command_topic()
        else:
            self._respond(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# Backend: wires everything together and runs the embedded HTTP server.
# ---------------------------------------------------------------------------

class Backend:
    """Owns the rclpy node and the components that serve from it, and runs the
    ThreadingHTTPServer that drives the whole process (media requests plus the
    favicon-free API). The separate translator classes above keep concerns
    apart: MediaStore (data), Publisher (ROS output), Player (playback),
    _Handler (HTTP)."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080,
                 command_topic: str = "/media_player/commands",
                 data_dir: str | None = None):
        self.host = host
        self.port = port
        self.command_topic = command_topic

        if data_dir is None:
            data_dir = os.path.expanduser("~/.ros/media_player")
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))

        self.node = rclpy.create_node("media_player")
        self.log = self.node.get_logger()

        self.store = MediaStore(self.data_dir, self.log)
        # QoS: reliable + transient local so the publisher can be created here
        # even if no subscriber exists yet (command_topic, legacy /publish).
        self.pub = self.node.create_publisher(String, command_topic, 10)
        self.publisher = Publisher(self.node)
        self.player = Player(self.publisher, self.log)
        self.web_dir = self._resolve_web_dir()

        self.log.info(f"media_player publishing commands on '{command_topic}'")
        self.log.info(f"media_player gallery at '{self.store.media_dir}'")
        self.log.info(
            "media_player web frontend "
            + (f"at '{self.web_dir}'" if self.web_dir
               else "NOT FOUND (build with `pnpm build` in frontend/, or set $MEDIA_PLAYER_WEB)"))

    @staticmethod
    def _resolve_web_dir() -> str | None:
        """Locate the built frontend (dist) to serve, or None if absent.

        Resolution order:
          1. $MEDIA_PLAYER_WEB  (explicit override)
          2. the dist colcon installs under share/ros_media_player/web
          3. frontend/dist next to the source tree
          4. ./frontend/dist  (relative to the working directory)
        """
        env = os.environ.get("MEDIA_PLAYER_WEB")
        if env:
            env = os.path.expanduser(env)
            if os.path.isfile(os.path.join(env, "index.html")):
                return os.path.abspath(env)
        try:
            from ament_index_python.packages import get_package_share_directory
            cand = os.path.join(get_package_share_directory("ros_media_player"), "web")
            if os.path.isfile(os.path.join(cand, "index.html")):
                return os.path.abspath(cand)
        except Exception:
            pass
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(here, "..", "frontend", "dist"),
                     os.path.join(here, "frontend", "dist"),
                     "frontend/dist"):
            cand = os.path.abspath(cand)
            if os.path.isfile(os.path.join(cand, "index.html")):
                return cand
        return None

    def start(self) -> None:
        """Start the playback thread and serve HTTP until interrupted."""
        self.player.start()
        httpd = ThreadingHTTPServer(
            (self.host, self.port),
            lambda *a, **k: _Handler(self, *a, **k))
        self.log.info(
            f"media_player web backend listening on http://{self.host}:{self.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            self.shutdown()

    def shutdown(self) -> None:
        if self.player.is_alive():
            self.player.shutdown()
            self.player.join(timeout=2.0)
        self.node.destroy_node()


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

    backend = Backend(host=host, port=port, command_topic=topic, data_dir=data_dir)
    try:
        backend.start()
    finally:
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()