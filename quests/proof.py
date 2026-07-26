"""Server-side EXIF extraction and quest-window validation for photo proofs.

The uploaded bytes are the only thing we trust from the client: capture time
and GPS are parsed here with Pillow, never taken from client-sent metadata.

EXIF capture times are naive local time on the device. When the photo carries
an OffsetTimeOriginal we use it; otherwise we assume UTC and rely on the
generous STALE_SLACK window below — the point of the automatic check is to
bounce photos that are *far* outside the quest window (yesterday's gallery
shot re-uploaded), while moderators judge the fine-grained cases from the
stored EXIF.
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone
from PIL import ExifTags, Image

# How far outside the quest window an EXIF capture time may fall before the
# submission is rejected outright. Also absorbs device-timezone ambiguity.
STALE_SLACK = timedelta(hours=12)

MAX_IMAGE_BYTES = 15 * 1024 * 1024

_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME = 0x0132
_TAG_OFFSET_ORIGINAL = 0x9011
_TAG_OFFSET = 0x9010

# EXIF keys worth keeping for the moderation view (readable subset).
_KEPT_TAGS = {
    0x010F: 'make',
    0x0110: 'model',
    0x0132: 'datetime',
    0x9003: 'datetime_original',
    0x9011: 'offset_time_original',
    0xA002: 'pixel_width',
    0xA003: 'pixel_height',
    0x8827: 'iso',
}


def _parse_exif_datetime(value, offset):
    """'2026:07:26 14:03:12' (+ optional '+05:00' offset) → aware datetime."""
    if not isinstance(value, str):
        return None
    try:
        naive = datetime.strptime(value.strip()[:19], '%Y:%m:%d %H:%M:%S')
    except ValueError:
        return None
    if isinstance(offset, str):
        try:
            sign = -1 if offset.strip()[0] == '-' else 1
            hh, mm = offset.strip().lstrip('+-').split(':')[:2]
            tz = dt_timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
            return naive.replace(tzinfo=tz)
        except (ValueError, IndexError):
            pass
    return naive.replace(tzinfo=dt_timezone.utc)


def _gps_to_degrees(triplet, ref):
    try:
        deg, minutes, seconds = (float(x) for x in triplet)
        value = deg + minutes / 60.0 + seconds / 3600.0
        if ref in ('S', 'W'):
            value = -value
        return round(value, 8)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def extract_exif(django_file):
    """Parse the uploaded image. Returns (info dict, error string or None).

    info = {capture_time, latitude, longitude, raw} — every field may be None
    except raw. error is set only when the file is not a readable image.
    """
    info = {'capture_time': None, 'latitude': None, 'longitude': None, 'raw': {}}
    try:
        django_file.seek(0)
        img = Image.open(django_file)
        img.verify()
        django_file.seek(0)
        img = Image.open(django_file)
        exif = img.getexif()
    except Exception:
        return info, 'Not a valid image file.'

    sub = {}
    gps = {}
    try:
        sub = dict(exif.get_ifd(ExifTags.IFD.Exif))
    except Exception:
        pass
    try:
        gps = dict(exif.get_ifd(ExifTags.IFD.GPSInfo))
    except Exception:
        pass
    base = dict(exif)
    merged = {**base, **sub}

    offset = merged.get(_TAG_OFFSET_ORIGINAL) or merged.get(_TAG_OFFSET)
    dt_value = merged.get(_TAG_DATETIME_ORIGINAL) or merged.get(_TAG_DATETIME)
    info['capture_time'] = _parse_exif_datetime(dt_value, offset)

    # GPSInfo: 1/2 = lat ref/value, 3/4 = lng ref/value
    if gps:
        info['latitude'] = _gps_to_degrees(gps.get(2), gps.get(1))
        info['longitude'] = _gps_to_degrees(gps.get(4), gps.get(3))

    raw = {}
    for tag, name in _KEPT_TAGS.items():
        value = merged.get(tag)
        if value is not None:
            raw[name] = str(value)[:120]
    if info['latitude'] is not None:
        raw['gps'] = f"{info['latitude']}, {info['longitude']}"
    info['raw'] = raw

    django_file.seek(0)
    return info, None


def quest_window(quest, now=None):
    """(start, end) aware datetimes of the quest's current active window."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    if quest.quest_type == quest.QuestType.DAILY:
        start_date = today
        length = timedelta(days=1)
    else:
        start_date = today - timedelta(days=today.weekday())  # ISO Monday
        length = timedelta(days=7)
    start = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
    return start, start + length


def capture_time_ok(capture_time, window_start, now=None):
    """False only when EXIF says the photo is far outside the active window.

    Missing capture time passes the automatic check — some devices strip
    EXIF — and is left for the moderator, who sees the absence flagged.
    """
    if capture_time is None:
        return True
    now = now or timezone.now()
    return window_start - STALE_SLACK <= capture_time <= now + STALE_SLACK
