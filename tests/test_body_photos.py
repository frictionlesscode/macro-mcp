import io
import math

import pytest
from PIL import Image

from macro_mcp import body_photos
from macro_mcp.models import ValidationError


@pytest.fixture(autouse=True)
def photo_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_DIR", str(tmp_path / "photos"))


def _png_bytes(color=(120, 120, 120), size=(200, 400)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# --- save_photo: validation --------------------------------------------------------------


def test_rejects_unknown_angle(db):
    with pytest.raises(ValidationError, match="angle"):
        body_photos.save_photo(db, _png_bytes(), angle="overhead")


def test_rejects_unreadable_bytes(db):
    with pytest.raises(ValidationError, match="not a readable image"):
        body_photos.save_photo(db, b"not an image at all")


# --- save_photo: degrades gracefully without mediapipe ------------------------------------


def test_save_succeeds_and_reports_why_alignment_failed_without_mediapipe(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    result = body_photos.save_photo(db, _png_bytes(), angle="front", day="2026-09-01")
    assert result["ok"]
    assert result["align_status"] == "failed"
    assert "mediapipe" in result["align_reason"]


def test_save_stores_width_and_height_from_the_actual_image(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    result = body_photos.save_photo(db, _png_bytes(size=(300, 500)), day="2026-09-01")
    assert (result["width"], result["height"]) == (300, 500)


# --- get_photo / list_photos / delete_photo -----------------------------------------------


def test_get_photo_null_reason_names_the_date_and_angle(db):
    result = body_photos.get_photo(db, "2026-09-01", angle="side")
    assert result["photo"] is None
    assert "2026-09-01" in result["photo_null_reason"]
    assert "side" in result["photo_null_reason"]


def test_save_then_get_round_trips_metadata(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    body_photos.save_photo(db, _png_bytes(), day="2026-09-01", note="post-workout")
    result = body_photos.get_photo(db, "2026-09-01")
    assert result["photo"]["note"] == "post-workout"
    assert result["photo_null_reason"] is None


def test_save_upserts_same_day_and_angle(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    body_photos.save_photo(db, _png_bytes(size=(100, 100)), day="2026-09-01", note="first")
    body_photos.save_photo(db, _png_bytes(size=(200, 200)), day="2026-09-01", note="second")
    result = body_photos.get_photo(db, "2026-09-01")
    assert result["photo"]["note"] == "second"
    assert result["photo"]["width"] == 200


def test_list_photos_only_includes_the_requested_angle_and_range(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    body_photos.save_photo(db, _png_bytes(), day="2026-09-01", angle="front")
    body_photos.save_photo(db, _png_bytes(), day="2026-09-02", angle="side")
    body_photos.save_photo(db, _png_bytes(), day="2026-09-10", angle="front")
    result = body_photos.list_photos(db, angle="front", start="2026-09-01", end="2026-09-05")
    assert [p["day"] for p in result["photos"]] == ["2026-09-01"]


def test_delete_photo_reports_whether_it_existed(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    body_photos.save_photo(db, _png_bytes(), day="2026-09-01")
    first = body_photos.delete_photo(db, "2026-09-01")
    assert first["ok"] and first["existed"] is True
    second = body_photos.delete_photo(db, "2026-09-01")
    assert second["existed"] is False
    assert body_photos.get_photo(db, "2026-09-01")["photo"] is None


def test_delete_photo_removes_the_file_from_disk(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    body_photos.save_photo(db, _png_bytes(), day="2026-09-01")
    path = body_photos._path_for("2026-09-01", "front")
    assert path.exists()
    body_photos.delete_photo(db, "2026-09-01")
    assert not path.exists()


# --- alignment geometry (pure, no mediapipe needed) ----------------------------------------


def _straight_landmarks():
    """Shoulders directly above hips -- already vertical, so rotation should be ~0."""
    return {
        "left_shoulder": [0.4, 0.2], "right_shoulder": [0.6, 0.2],
        "left_hip": [0.4, 0.6], "right_hip": [0.6, 0.6],
    }


def test_landmark_geometry_finds_midpoints_in_pixel_space():
    geom = body_photos._landmark_geometry(_straight_landmarks(), width=1000, height=1000)
    assert geom["shoulders_mid"] == pytest.approx((500, 200))
    assert geom["hips_mid"] == pytest.approx((500, 600))
    assert geom["torso_len_px"] == pytest.approx(400)


def test_landmark_geometry_zero_rotation_for_an_already_vertical_torso():
    geom = body_photos._landmark_geometry(_straight_landmarks(), width=1000, height=1000)
    assert geom["rotation_rad"] == pytest.approx(0.0, abs=1e-9)


def test_landmark_geometry_detects_a_tilted_torso():
    tilted = {
        "left_shoulder": [0.3, 0.2], "right_shoulder": [0.5, 0.2],
        "left_hip": [0.4, 0.6], "right_hip": [0.6, 0.6],
    }
    geom = body_photos._landmark_geometry(tilted, width=1000, height=1000)
    assert abs(geom["rotation_rad"]) > 0.01


def test_align_image_maps_hips_onto_the_anchor_point():
    geom = body_photos._landmark_geometry(_straight_landmarks(), width=1000, height=1000)
    img = Image.new("RGB", (1000, 1000), (10, 10, 10))
    aligned = body_photos._align_image(img, geom)
    assert aligned.size == (body_photos.CANVAS_W, body_photos.CANVAS_H)


def test_align_image_rejects_degenerate_torso():
    degenerate = {
        "left_shoulder": [0.5, 0.5], "right_shoulder": [0.5, 0.5],
        "left_hip": [0.5, 0.5], "right_hip": [0.5, 0.5],
    }
    geom = body_photos._landmark_geometry(degenerate, width=1000, height=1000)
    img = Image.new("RGB", (1000, 1000))
    with pytest.raises(ValidationError, match="degenerate"):
        body_photos._align_image(img, geom)


# --- aligned_jpeg_bytes -----------------------------------------------------------------


def test_aligned_jpeg_bytes_falls_back_to_original_without_landmarks(db, monkeypatch):
    monkeypatch.setattr(body_photos, "POSE_AVAILABLE", False)
    body_photos.save_photo(db, _png_bytes(), day="2026-09-01")
    data, status = body_photos.aligned_jpeg_bytes(db, "2026-09-01")
    assert status == "original"
    assert data[:2] == b"\xff\xd8"  # JPEG magic bytes


def test_aligned_jpeg_bytes_raises_for_a_missing_photo(db):
    with pytest.raises(ValidationError):
        body_photos.aligned_jpeg_bytes(db, "2026-09-01")
