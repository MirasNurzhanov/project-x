"""Bounded media validation. Filenames and Content-Type are server controlled."""
from io import BytesIO
import struct
import uuid
import warnings

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
IMAGE_FORMATS = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp')}


def _size(upload, limit):
    upload.seek(0, 2)
    size = upload.tell()
    upload.seek(0)
    if size <= 0 or size > limit:
        raise ValidationError(f'File must be between 1 byte and {limit // (1024 * 1024)}MB.')
    return size


def validate_image(upload):
    _size(upload, MAX_IMAGE_BYTES)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(upload) as probe:
                fmt = probe.format
                if fmt not in IMAGE_FORMATS or getattr(probe, 'n_frames', 1) != 1:
                    raise ValidationError('Use a still JPEG, PNG or WebP image.')
                if max(probe.size) > 8192 or probe.width * probe.height > MAX_IMAGE_PIXELS:
                    raise ValidationError('Image dimensions exceed the 16 megapixel / 8192 pixel limit.')
                probe.verify()
            upload.seek(0)
            with Image.open(upload) as decoded:
                decoded.load()
                oriented = ImageOps.exif_transpose(decoded)
                mode = 'RGBA' if fmt != 'JPEG' and ('A' in oriented.getbands() or 'transparency' in oriented.info) else 'RGB'
                # A fresh raster strips EXIF, comments and trailing/polyglot bytes.
                clean = Image.new(mode, oriented.size)
                clean.paste(oriented.convert(mode))
                output = BytesIO()
                clean.save(output, format=fmt)
        if output.tell() > MAX_IMAGE_BYTES:
            raise ValidationError('Decoded image is too large. Choose a smaller image.')
        extension, mime = IMAGE_FORMATS[fmt]
        return SimpleUploadedFile(f'{uuid.uuid4().hex}.{extension}', output.getvalue(), content_type=mime)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError('The file is not a valid supported image.') from None
    finally:
        upload.seek(0)


def validate_video(upload):
    """Validate a bounded ISO-BMFF container, not codecs or moderation safety."""
    from swifts.media import MAX_UPLOAD_BYTES
    size = _size(upload, MAX_UPLOAD_BYTES)
    count = 0

    def boxes(start, end):
        nonlocal count
        position = start
        while position < end:
            count += 1
            if count > 2048 or end - position < 8:
                raise ValueError()
            upload.seek(position)
            length, kind = struct.unpack('>I4s', upload.read(8))
            header = 8
            if length == 1:
                length = struct.unpack('>Q', upload.read(8))[0]
                header = 16
            elif length == 0:
                length = end - position
            if length < header or position + length > end:
                raise ValueError()
            yield kind, position + header, position + length
            position += length

    try:
        top = list(boxes(0, size))
        if not top or top[0][0] != b'ftyp':
            raise ValueError()
        _, start, end = top[0]
        if end - start < 8 or end - start > 4096:
            raise ValueError()
        upload.seek(start)
        brand = upload.read(4)
        if brand not in (b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'qt  ', b'M4V '):
            raise ValueError()
        has_data = any(kind == b'mdat' and end > start for kind, start, end in top)
        has_video = False
        for kind, start, end in top:
            if kind != b'moov':
                continue
            for kind, start, end in boxes(start, end):
                if kind != b'trak':
                    continue
                for kind, start, end in boxes(start, end):
                    if kind != b'mdia':
                        continue
                    for kind, start, end in boxes(start, end):
                        if kind == b'hdlr' and end - start >= 12:
                            upload.seek(start + 8)
                            has_video |= upload.read(4) == b'vide'
        if not has_data or not has_video:
            raise ValueError()
        quicktime = brand == b'qt  '
        upload.name = f'{uuid.uuid4().hex}.{"mov" if quicktime else "mp4"}'
        upload.content_type = 'video/quicktime' if quicktime else 'video/mp4'
        return upload
    except (ValueError, struct.error, OSError):
        raise ValidationError('Use a valid MP4 or QuickTime video file.') from None
    finally:
        upload.seek(0)
