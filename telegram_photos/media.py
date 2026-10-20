"""Classify Telegram attachments without fetching their bytes or link previews."""

from dataclasses import dataclass
from pathlib import PurePosixPath

from telethon.tl import types

IMAGE_EXTENSIONS = set("""
    .avif .bmp .gif .heic .heif .jp2 .jxl .jpg .jpeg .jpe .jfif .insp .mpo
    .png .psd .raw .rw2 .tif .tiff .webp .dng .cr2 .cr3 .nef .arw .orf .raf
""".split())
VIDEO_EXTENSIONS = set("""
    .3gp .3gpp .avi .flv .m4v .mkv .mts .m2ts .m2t .ts .mp4 .insv
    .mpg .mpe .mpeg .mxf .mov .webm .wmv
""".split())
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif", "image/avif": ".avif",
    "image/tiff": ".tiff", "image/gif": ".gif", "image/bmp": ".bmp",
    "image/x-ms-bmp": ".bmp", "image/jp2": ".jp2", "image/jxl": ".jxl",
    "image/vnd.adobe.photoshop": ".psd", "image/x-adobe-dng": ".dng",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-m4v": ".m4v",
    "video/x-matroska": ".mkv", "video/webm": ".webm", "video/x-msvideo": ".avi",
    "video/avi": ".avi", "video/mpeg": ".mpeg", "video/mp2t": ".ts",
    "video/3gpp": ".3gp", "video/x-flv": ".flv", "video/x-ms-wmv": ".wmv",
    "application/mxf": ".mxf",
}
GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream", "application/binary"}


@dataclass(frozen=True)
class Attachment:
    part: str
    extension: str
    kind: str
    target: object
    size: int | None = None


def document_attachment(document, target, part="main", media=None):
    attributes = getattr(document, "attributes", []) or []
    if any(isinstance(attr, (types.DocumentAttributeSticker, types.DocumentAttributeCustomEmoji))
           for attr in attributes):
        return None, "sticker"
    mime = (getattr(document, "mime_type", "") or "").split(";", 1)[0].strip().lower()
    if mime.startswith("audio/") or getattr(media, "voice", False) or any(
        isinstance(attr, types.DocumentAttributeAudio) for attr in attributes
    ):
        return None, "audio"
    if not document or isinstance(document, types.DocumentEmpty):
        return None, "unavailable"
    filename = next((attr.file_name for attr in attributes if isinstance(attr, types.DocumentAttributeFilename)), "")
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    video = next((attr for attr in attributes if isinstance(attr, types.DocumentAttributeVideo)), None)
    animated = any(isinstance(attr, types.DocumentAttributeAnimated) for attr in attributes)
    if mime.startswith("image/"):
        extension = suffix if suffix in IMAGE_EXTENSIONS else MIME_EXTENSIONS.get(mime)
    elif mime.startswith("video/") or mime == "application/mxf":
        extension = suffix if suffix in VIDEO_EXTENSIONS else MIME_EXTENSIONS.get(mime)
    elif mime in GENERIC_MIMES:
        extension = suffix if suffix in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS else None
        if extension is None and (video or getattr(media, "video", False) or getattr(media, "round", False)):
            extension = ".mp4"  # Native Telegram video without a filename.
    else:
        extension = None
    if extension is None:
        return None, "unsupported_file"
    kind = "photo" if extension in IMAGE_EXTENSIONS else "video"
    if extension == ".gif" or animated:
        kind = "animation"
    elif getattr(video, "round_message", False) or getattr(media, "round", False):
        kind = "video_note"
    return Attachment(part, extension, kind, target, getattr(document, "size", None)), None


def attachments(msg):
    """Return all original assets plus any reason for skipping a message/component."""
    media = getattr(msg, "media", None)
    if getattr(msg, "noforwards", False):
        return [], "protected"
    if getattr(msg, "video_processing_pending", False):
        return [], "pending_video"
    if getattr(media, "ttl_seconds", None):
        return [], "self_destructing"
    if isinstance(media, types.MessageMediaPhoto):
        if not isinstance(media.photo, types.Photo):
            return [], "unavailable"
        size = getattr(getattr(msg, "file", None), "size", None)
        items = [Attachment("main", ".jpg", "photo", msg, size)]
        if getattr(media, "live_photo", False):
            clip, reason = document_attachment(media.video, media.video, "live_video")
            if clip:
                items.append(clip)
            else:
                return items, reason
        return items, None
    if isinstance(media, types.MessageMediaDocument):
        if getattr(msg, "sticker", None):
            return [], "sticker"
        item, reason = document_attachment(getattr(msg, "document", None) or media.document, msg, media=media)
        return ([item] if item else []), reason
    if media is None or isinstance(media, types.MessageMediaEmpty):
        return [], "service" if getattr(msg, "action", None) else "text"
    if isinstance(media, types.MessageMediaWebPage):
        return [], "link_preview"
    if isinstance(media, types.MessageMediaPaidMedia):
        return [], "paid_media"
    if isinstance(media, types.MessageMediaStory):
        return [], "story"
    return [], "other"
