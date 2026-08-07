"""Image downscaling on upload.

Every turn re-sends every image, so the bytes stored here are paid again on
every future message in that chat. These tests pin the size win and, more
importantly, the cases where converting would LOSE something and must not run.
"""

import io

import pytest
from PIL import Image

from backend import images

pytestmark = pytest.mark.skipif(not hasattr(images, "Image"),
                                reason="Pillow not installed")


def _photo(w=4032, h=3024, fmt="JPEG", mode="RGB", color=(120, 90, 60)):
    """A photo-shaped image with enough noise that JPEG can't cheat it small."""
    img = Image.new(mode, (w, h), color)
    px = img.load()
    for x in range(0, w, 7):
        for y in range(0, h, 7):
            px[x, y] = ((x * 3) % 255, (y * 5) % 255, (x + y) % 255)[:len(mode)] \
                if mode != "RGBA" else ((x * 3) % 255, (y * 5) % 255, (x + y) % 255, 255)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def test_phone_photo_shrinks_hard_and_stays_within_model_resolution():
    original = _photo()
    out = images.downscale(original, "image/jpeg", "IMG_7924.jpeg")
    assert out is not None
    assert max(out["width"], out["height"]) == images.MAX_EDGE
    # the point of the change: an order-of-magnitude smaller payload per turn
    assert len(out["data"]) < len(original) / 4


def test_image_already_within_model_resolution_is_left_alone():
    """Tokens scale with dimensions, not bytes - re-encoding something already
    small saves nothing and only costs quality (worst for text screenshots)."""
    assert images.downscale(_photo(w=400, h=300), "image/jpeg", "thumb.jpg") is None
    assert images.downscale(_photo(w=1568, h=1000), "image/jpeg", "ok.jpg") is None


def test_transparent_png_stays_png():
    """Flattening alpha would visibly wreck a screenshot or logo."""
    img = Image.new("RGBA", (3000, 2000), (255, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = images.downscale(buf.getvalue(), "image/png", "logo.png")
    assert out is not None and out["mime"] == "image/png"


def test_animated_gif_is_never_touched():
    """Converting an animation to a still is data loss nobody asked for."""
    frames = [Image.new("P", (2000, 2000), i) for i in (0, 1)]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    assert images.downscale(buf.getvalue(), "image/gif", "spin.gif") is None


def test_exif_orientation_is_baked_in_not_dropped():
    """Strip EXIF without applying it and portrait phone photos come out
    sideways - the classic re-encode bug. Only reachable on images we actually
    re-encode; smaller ones keep their own EXIF untouched."""
    img = Image.new("RGB", (4000, 3000), (10, 20, 30))
    exif = img.getexif()
    exif[274] = 6  # Orientation: rotate 90°
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out = images.downscale(buf.getvalue(), "image/jpeg", "portrait.jpg")
    assert out is not None
    # landscape 4000x3000 with orientation=6 must come back portrait
    assert out["height"] > out["width"]


def test_capture_date_is_read_before_metadata_is_stripped():
    img = Image.new("RGB", (3000, 2000), (5, 5, 5))
    exif = img.getexif()
    exif[306] = "2026:07:31 15:04:22"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out = images.downscale(buf.getvalue(), "image/jpeg", "shot.jpg")
    assert out is not None and out["capture_date"].startswith("2026-07-31")


def test_gps_and_device_metadata_do_not_survive():
    """Those bytes used to travel to two providers on every turn."""
    img = Image.new("RGB", (3000, 2000), (5, 5, 5))
    exif = img.getexif()
    exif[271] = "SecretCameraMake"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out = images.downscale(buf.getvalue(), "image/jpeg", "shot.jpg")
    assert b"SecretCameraMake" not in out["data"]


def test_corrupt_bytes_never_cost_the_user_their_upload():
    assert images.downscale(b"not an image at all", "image/jpeg", "broken.jpg") is None


def test_non_images_are_ignored():
    assert images.downscale(b"%PDF-1.4 ...", "application/pdf", "doc.pdf") is None
    assert images.downscale(b"hello", "text/plain", "notes.txt") is None


@pytest.mark.skipif(not images.HEIF_OK, reason="pillow-heif not installed")
def test_heic_converts_to_jpeg_so_iphone_photos_stop_being_rejected():
    img = Image.new("RGB", (3000, 2000), (100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, "HEIF")
    out = images.downscale(buf.getvalue(), "image/heic", "IMG_0001.HEIC")
    assert out is not None
    assert out["mime"] == "image/jpeg" and out["filename"].endswith(".jpg")


def test_heic_converts_even_when_the_result_is_not_smaller():
    """The never-grow guard must not strip HEIC of its conversion: no provider
    accepts HEIC, so an unconverted one is an unusable attachment."""
    tiny = Image.new("RGB", (60, 40), (1, 2, 3))
    buf = io.BytesIO()
    if not images.HEIF_OK:
        pytest.skip("pillow-heif not installed")
    tiny.save(buf, "HEIF")
    out = images.downscale(buf.getvalue(), "image/heic", "tiny.heic")
    assert out is not None and out["mime"] == "image/jpeg"


def test_heic_is_accepted_by_the_upload_type_gate():
    from backend import attachments as att

    assert att.kind_of("IMG_0001.HEIC", "image/heic") == "image"
    assert att.kind_of("IMG_0001.heif", "application/octet-stream") == "image"
