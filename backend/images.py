"""Downscale photos on upload, because every turn re-sends every image.

The provider APIs are stateless: each turn rebuilds the whole transcript and
re-attaches every image. Half a dozen photos on message 31 are half a dozen
photos sent thirty-one times, so a chat that picks up a few camera-roll uploads
gets slower and more expensive with every message until it is unusable, while
the typed text beside those photos stays a rounding error.

The size is pure waste. Vision models downscale to roughly 1568px on the long
edge before they look at anything, so a 5 MB phone photo and a 400 KB
downscaled one yield the SAME tokens and the same visible detail. We were
paying upload time, cache churn and latency for pixels no model ever sees.

So: convert and downscale once, on upload, and store that. The original is not
kept, deliberately: nothing ever reads it in another form (the chat view serves
the same stored file, and the models can't use the extra pixels), so a second
copy would buy nothing but disk. The owner's call, recorded in DECISIONS.md.

Two things the original carries that would otherwise be lost, so they are
handled explicitly:

* **EXIF orientation** is baked in before stripping, or re-encoded phone photos
  come out rotated.
* **Capture date** is read out before the metadata goes, because when a photo
  was taken is real information about it. The rest of EXIF (GPS, device serial,
  lens data) is dropped, which is a privacy improvement: those bytes used to go
  to every model provider on every single turn.

HEIC support arrives with the same dependency, so iPhone photos stop being
rejected as an unsupported type.
"""

import io
import logging

from PIL import Image, ImageOps

try:  # HEIC/HEIF: iPhone's default format, previously rejected outright
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover - optional dependency
    HEIF_OK = False

log = logging.getLogger("crossband.images")

# Anthropic and OpenAI both downscale beyond ~1568px on the long edge before
# tokenising. Storing anything larger buys zero model-visible detail.
MAX_EDGE = 1568

# Re-encode quality. 85 is the usual "indistinguishable at normal viewing"
# point for photographs and roughly a tenth the bytes of a phone original.
JPEG_QUALITY = 85

# Formats worth re-encoding. GIF is excluded on purpose: it may be animated,
# and flattening someone's animation to a still is a data loss they did not ask
# for. WebP and PNG are converted to JPEG only when they are photographs
# (see _target_format) so screenshots and diagrams keep their crisp edges.
CONVERTIBLE = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def is_convertible(mime: str, filename: str = "") -> bool:
    name = (filename or "").lower()
    return (mime or "").lower() in CONVERTIBLE or name.endswith((".heic", ".heif"))


def _target_format(img: Image.Image, mime: str) -> tuple[str, str]:
    """(PIL format, mime) for the output.

    PNGs with transparency stay PNG - flattening alpha onto a background would
    visibly change a screenshot or a logo. Everything else becomes JPEG, which
    is what phone photographs already are.
    """
    if (mime or "").lower() == "image/png" and img.mode in ("RGBA", "LA", "P"):
        if "transparency" in img.info or img.mode in ("RGBA", "LA"):
            return "PNG", "image/png"
    return "JPEG", "image/jpeg"


def capture_date(img: Image.Image) -> str | None:
    """EXIF DateTimeOriginal as 'YYYY-MM-DD HH:MM:SS', if the camera wrote one.

    Read before re-encoding strips it: when a photo was taken is information
    about the photo, and it is the one EXIF field worth keeping.
    """
    try:
        exif = img.getexif()
        if not exif:
            return None
        # 36867 DateTimeOriginal, 306 DateTime (fallback)
        raw = exif.get(36867) or exif.get(306)
        if not raw:
            ifd = exif.get_ifd(0x8769)  # ExifIFD
            raw = ifd.get(36867) if ifd else None
        if not raw:
            return None
        text = str(raw).strip()
        # EXIF writes "2026:01:15 15:04:22"; normalise the date half only.
        if len(text) >= 19 and text[4] == ":" and text[7] == ":":
            return text[:4] + "-" + text[5:7] + "-" + text[8:]
        return text
    except Exception:
        return None


def downscale(data: bytes, mime: str, filename: str = "") -> dict | None:
    """Return {data, mime, filename, width, height, capture_date} or None.

    ``None`` means "store the original unchanged" - not an image we convert, or
    anything went wrong. Failure must never cost the user their upload.
    """
    if not is_convertible(mime, filename):
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        log.warning("image open failed (%s) - storing original: %s", filename, exc)
        return None

    was_heic = (mime or "").lower() in ("image/heic", "image/heif") or \
        (filename or "").lower().endswith((".heic", ".heif"))

    # Only touch an image when it actually buys something. Token cost scales
    # with DIMENSIONS, not bytes, so re-encoding a picture already within the
    # model's resolution saves no tokens and only costs quality - which matters
    # most for the screenshots of text people paste. Convert when the image is
    # too big to be sent whole, or when the format is unusable (HEIC).
    try:
        if max(img.size) <= MAX_EDGE and not was_heic:
            return None
    except Exception:
        return None

    try:
        taken = capture_date(img)
        # Bake in EXIF orientation BEFORE stripping metadata, or portrait phone
        # photos come back sideways.
        img = ImageOps.exif_transpose(img)

        fmt, out_mime = _target_format(img, mime)
        if fmt == "JPEG" and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        width, height = img.size
        if max(width, height) > MAX_EDGE:
            img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

        buf = io.BytesIO()
        if fmt == "JPEG":
            img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
        else:
            img.save(buf, "PNG", optimize=True)
        out = buf.getvalue()
    except Exception as exc:
        log.warning("image downscale failed (%s) - storing original: %s", filename, exc)
        return None

    # Never make a file bigger. A detailed PNG can re-encode larger; in that
    # case the original is the better thing to store - unless it is HEIC, which
    # no provider accepts and must be converted regardless.
    if len(out) >= len(data) and not was_heic:
        return None

    name = filename or "image"
    if out_mime == "image/jpeg" and not name.lower().endswith((".jpg", ".jpeg")):
        name = name.rsplit(".", 1)[0] + ".jpg"
    return {"data": out, "mime": out_mime, "filename": name,
            "width": img.size[0], "height": img.size[1], "capture_date": taken}
