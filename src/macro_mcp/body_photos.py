"""Body progress photos: storage, pose-landmark alignment, and series retrieval.

Stored on disk under ``PHOTO_DIR`` (default: a ``photos/`` sibling of the sqlite file),
one photo per ``(day, angle)`` -- a later save overwrites, matching ``day_target``'s
upsert-by-date pattern. EXIF is stripped on save (a fresh JPEG re-encode drops it):
photos of a body carry more privacy risk than a food photo, and EXIF can carry GPS.

Alignment finds the shoulder and hip midpoints via MediaPipe's classic Pose "Solutions"
API (bundled weights, no model download needed -- important for a box with no guaranteed
internet access after first boot), then rotates/scales/translates each photo onto a
shared canvas so the torso lines up frame to frame. That's what makes a small change
visible in a slideshow instead of buried under photo-to-photo differences in distance,
tilt, and framing.

The geometry (``_landmark_geometry`` / ``_affine_coeffs``) is pure and unit-tested without
MediaPipe. MediaPipe itself is an optional dependency (see pyproject.toml's ``photos``
extra) -- its absence, or a failed detection on one photo (bad angle, occlusion, low
light), is not a hard failure: the photo is still stored and still shown, just unaligned
and flagged with a reason, consistent with this project's fail-closed / honest-null rule
elsewhere (SPEC.md).
"""

from __future__ import annotations

import io
import json
import math
import os
import sqlite3
from datetime import date as Date
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .models import PHOTO_ANGLES, ValidationError, iso, now, require, today
from .store import db_path, transaction

#: Canvas every aligned photo is rendered onto, and the torso length (shoulder-midpoint to
#: hip-midpoint, in pixels) every photo is scaled to match. Arbitrary but fixed -- what
#: matters is that every photo in a series shares the same numbers.
CANVAS_W, CANVAS_H = 640, 960
TARGET_TORSO_PX = 260.0
#: Where the hip midpoint lands on the canvas after alignment, as a fraction of (W, H).
ANCHOR_FRAC = (0.5, 0.62)

_LANDMARK_INDEX = {"left_shoulder": 11, "right_shoulder": 12, "left_hip": 23, "right_hip": 24}
_VISIBILITY_MIN = 0.5

#: mediapipe's classic "Solutions" Pose API (mp.solutions.pose) -- bundled weights, no model
#: download -- was removed entirely as of mediapipe 1.x (confirmed against the real installed
#: package: `mp.solutions` no longer exists). Only the newer Tasks API remains, and it needs
#: an actual model file on disk; there is no way around downloading one. POSE_MODEL_PATH
#: points at it -- see Dockerfile, which fetches it at build time so the running container
#: never needs internet access for this.
_DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "models" / "pose_landmarker_lite.task"

try:
    import mediapipe as mp

    POSE_AVAILABLE = True
except ImportError:
    mp = None
    POSE_AVAILABLE = False

_landmarker = None
_landmarker_error: str | None = None


def _get_landmarker():
    """Lazily builds the one PoseLandmarker instance this process uses -- loading the model
    file isn't free, so this runs once, not per photo. A missing/broken model file is cached
    as an error rather than retried on every save; a symlink swap plus a process restart is
    the intended way to fix it.
    """
    global _landmarker, _landmarker_error
    if _landmarker is not None or _landmarker_error is not None:
        return _landmarker

    model_path = Path(os.environ.get("POSE_MODEL_PATH") or _DEFAULT_MODEL_PATH)
    if not model_path.exists():
        _landmarker_error = (
            f"pose model file not found at {model_path} -- see docs/self-hosted-setup.md "
            f"for how to fetch it, or set POSE_MODEL_PATH"
        )
        return None
    try:
        base_options = mp.tasks.BaseOptions(model_asset_path=str(model_path))
        options = mp.tasks.vision.PoseLandmarkerOptions(
            base_options=base_options,
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            num_poses=1,
        )
        _landmarker = mp.tasks.vision.PoseLandmarker.create_from_options(options)
    except Exception as exc:  # noqa: BLE001 -- a bad model file must degrade, not crash the server
        _landmarker_error = f"failed to load pose model at {model_path}: {exc}"
        return None
    return _landmarker


def photo_dir() -> Path:
    """Where photo JPEGs live on disk. Independently overridable via PHOTO_DIR (tests set
    this to a tmp dir -- db_path() ignores the actual path an in-memory test DB was opened
    with, so without this override tests would otherwise write into the real repo's
    ./data/photos).
    """
    override = os.environ.get("PHOTO_DIR")
    d = Path(override) if override else db_path().parent / "photos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_for(day: str, angle: str) -> Path:
    return photo_dir() / f"{day}_{angle}.jpg"


# --- pose detection (I/O-ish; not unit-tested directly, see module docstring) ----------


def _detect_landmarks(img: Image.Image) -> dict[str, list[float]] | None:
    """Returns normalized {name: [x, y]} for the four torso landmarks, or None if no
    confident full-body pose was found. Only called when POSE_AVAILABLE; raises if the
    model file itself can't be loaded (caught by save_photo's caller, which turns that into
    an honest align_reason rather than a crash).
    """
    import numpy as np

    landmarker = _get_landmarker()
    if landmarker is None:
        raise RuntimeError(_landmarker_error)

    rgb = np.array(img.convert("RGB"))
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = landmarker.detect(mp_image)
    if not result.pose_landmarks:
        return None
    lm = result.pose_landmarks[0]  # num_poses=1, so at most one detection
    out: dict[str, list[float]] = {}
    for name, idx in _LANDMARK_INDEX.items():
        point = lm[idx]
        if point.visibility < _VISIBILITY_MIN:
            return None
        out[name] = [point.x, point.y]
    return out


# --- alignment geometry (pure, unit-tested) ---------------------------------------------


def _landmark_geometry(landmarks: dict[str, list[float]], width: int, height: int) -> dict[str, Any]:
    """Pixel-space midpoints, torso length, and the rotation needed to point the torso
    straight up. Landmarks are normalized [0,1]; separated from image I/O so it can be
    tested with synthetic coordinates and no real pose model.
    """

    def px(name: str) -> tuple[float, float]:
        x, y = landmarks[name]
        return (x * width, y * height)

    ls, rs = px("left_shoulder"), px("right_shoulder")
    lh, rh = px("left_hip"), px("right_hip")
    shoulders_mid = ((ls[0] + rs[0]) / 2, (ls[1] + rs[1]) / 2)
    hips_mid = ((lh[0] + rh[0]) / 2, (lh[1] + rh[1]) / 2)
    dx = shoulders_mid[0] - hips_mid[0]
    dy = shoulders_mid[1] - hips_mid[1]
    torso_len = math.hypot(dx, dy)
    # angle from the torso vector to straight-up (0, -1) in image coords (y grows down)
    angle_rad = math.atan2(-1, 0) - math.atan2(dy, dx) if torso_len else 0.0
    return {
        "shoulders_mid": shoulders_mid,
        "hips_mid": hips_mid,
        "torso_len_px": torso_len,
        "rotation_rad": angle_rad,
    }


def _affine_coeffs(
    geom: dict[str, Any], scale: float, anchor_px: tuple[float, float]
) -> tuple[float, float, float, float, float, float]:
    """PIL's AFFINE data, mapping *output* pixels back to *input* pixels: the inverse of
    "rotate by geom's angle, scale, then translate hips_mid to anchor_px".
    """
    c, s = math.cos(geom["rotation_rad"]), math.sin(geom["rotation_rad"])
    inv_scale = 1.0 / scale
    a, b = inv_scale * c, inv_scale * s
    d, e = -inv_scale * s, inv_scale * c
    hx, hy = geom["hips_mid"]
    ax, ay = anchor_px
    c_ = hx - a * ax - b * ay
    f_ = hy - d * ax - e * ay
    return (a, b, c_, d, e, f_)


def _align_image(img: Image.Image, geom: dict[str, Any]) -> Image.Image:
    if geom["torso_len_px"] <= 1e-6:
        raise ValidationError("degenerate torso length; cannot align")
    scale = TARGET_TORSO_PX / geom["torso_len_px"]
    anchor_px = (ANCHOR_FRAC[0] * CANVAS_W, ANCHOR_FRAC[1] * CANVAS_H)
    coeffs = _affine_coeffs(geom, scale, anchor_px)
    return img.transform(
        (CANVAS_W, CANVAS_H), Image.AFFINE, coeffs, resample=Image.BICUBIC, fillcolor=(0, 0, 0)
    )


# --- storage -----------------------------------------------------------------------------


def _row_to_meta(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "day": row["day"],
        "angle": row["angle"],
        "width": row["width"],
        "height": row["height"],
        "align_status": row["align_status"],
        "align_reason": row["align_reason"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def save_photo(
    conn: sqlite3.Connection,
    image_bytes: bytes,
    angle: str = "front",
    day: Date | str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    require(angle, PHOTO_ANGLES, "angle")
    target = day.isoformat() if isinstance(day, Date) else (day or today().isoformat())
    Date.fromisoformat(target)  # validate shape if caller passed a string

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as exc:
        raise ValidationError(f"not a readable image: {exc}") from exc
    img = ImageOps.exif_transpose(img).convert("RGB")  # apply EXIF rotation, then it's gone
    width, height = img.size

    landmarks: dict[str, list[float]] | None = None
    if not POSE_AVAILABLE:
        align_status, align_reason = "failed", "mediapipe not installed on this host"
    else:
        try:
            landmarks = _detect_landmarks(img)
        except Exception as exc:  # noqa: BLE001 -- a bad detection must never block the save
            align_status, align_reason = "failed", f"pose detection failed: {exc}"
        else:
            if landmarks is None:
                align_status = "failed"
                align_reason = "no clear full-body pose detected in this photo"
            else:
                align_status, align_reason = "ok", None

    path = _path_for(target, angle)
    img.save(path, "JPEG", quality=90)

    stamp = iso(now())
    landmarks_json = json.dumps(landmarks) if landmarks else None
    with transaction(conn):
        conn.execute(
            """INSERT INTO body_photo
               (day, angle, file_path, width, height, landmarks_json, align_status,
                align_reason, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day, angle) DO UPDATE SET
                   file_path = excluded.file_path, width = excluded.width,
                   height = excluded.height, landmarks_json = excluded.landmarks_json,
                   align_status = excluded.align_status, align_reason = excluded.align_reason,
                   note = excluded.note, created_at = excluded.created_at""",
            (target, angle, path.name, width, height, landmarks_json, align_status,
             align_reason, note, stamp),
        )

    return {
        "ok": True,
        "day": target,
        "angle": angle,
        "width": width,
        "height": height,
        "align_status": align_status,
        "align_reason": align_reason,
    }


def get_photo(conn: sqlite3.Connection, day: str | None = None, angle: str = "front") -> dict[str, Any]:
    require(angle, PHOTO_ANGLES, "angle")
    target = day or today().isoformat()
    row = conn.execute(
        "SELECT * FROM body_photo WHERE day = ? AND angle = ?", (target, angle)
    ).fetchone()
    if row is None:
        return {
            "day": target,
            "angle": angle,
            "photo": None,
            "photo_null_reason": f"no {angle} photo stored for {target} (see log_body_photo)",
        }
    return {"day": target, "angle": angle, "photo": _row_to_meta(row), "photo_null_reason": None}


def list_photos(
    conn: sqlite3.Connection, angle: str = "front", start: str | None = None, end: str | None = None
) -> dict[str, Any]:
    require(angle, PHOTO_ANGLES, "angle")
    end_d = Date.fromisoformat(end) if end else today()
    start_d = Date.fromisoformat(start) if start else Date.fromordinal(end_d.toordinal() - 89)
    rows = conn.execute(
        "SELECT * FROM body_photo WHERE angle = ? AND day BETWEEN ? AND ? ORDER BY day",
        (angle, start_d.isoformat(), end_d.isoformat()),
    ).fetchall()
    return {
        "angle": angle,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "photos": [_row_to_meta(r) for r in rows],
    }


def delete_photo(conn: sqlite3.Connection, day: str, angle: str = "front") -> dict[str, Any]:
    require(angle, PHOTO_ANGLES, "angle")
    row = conn.execute(
        "SELECT 1 FROM body_photo WHERE day = ? AND angle = ?", (day, angle)
    ).fetchone()
    existed = row is not None
    if existed:
        with transaction(conn):
            conn.execute("DELETE FROM body_photo WHERE day = ? AND angle = ?", (day, angle))
        try:
            _path_for(day, angle).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "day": day, "angle": angle, "existed": existed}


# --- rendering support for the dashboard (see dashboard.py) -----------------------------


def aligned_jpeg_bytes(conn: sqlite3.Connection, day: str, angle: str = "front") -> tuple[bytes, str]:
    """The photo for (day, angle) as JPEG bytes, aligned onto the shared canvas when
    landmarks are available. Returns (bytes, status) where status is "aligned" or
    "original" -- the dashboard needs to know which, so it can label unaligned frames
    rather than silently mixing them into an otherwise-aligned slideshow.
    """
    row = conn.execute(
        "SELECT * FROM body_photo WHERE day = ? AND angle = ?", (day, angle)
    ).fetchone()
    if row is None:
        raise ValidationError(f"no {angle} photo stored for {day}")

    img = Image.open(_path_for(day, angle)).convert("RGB")
    if row["landmarks_json"]:
        landmarks = json.loads(row["landmarks_json"])
        geom = _landmark_geometry(landmarks, row["width"], row["height"])
        try:
            aligned = _align_image(img, geom)
        except ValidationError:
            pass
        else:
            buf = io.BytesIO()
            aligned.save(buf, "JPEG", quality=88)
            return buf.getvalue(), "aligned"

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue(), "original"
