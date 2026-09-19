"""Where a Swift's media lives, after the R2 migration.

## What used to be here

A Swift used to be a raw recording uploaded straight to Cloudinary, plus a set
of edit parameters (trim, volume, a music bed, a text overlay, a poster
timestamp) that this module turned into a Cloudinary delivery-transformation
URL — rendered lazily on first request, CDN-cached after. That existed because
none of the three places that could actually produce the composited file were
workable: not the phone (`ffmpeg-kit-react-native` is retired), not Django on
Railway (no task queue, no worker to run `ffmpeg` in), and Cloudinary's
URL-addressed transformations needed no infrastructure at all.

R2 is a plain object store. It cannot decode, mix or re-encode video, so it
cannot stand in for that transformation service — only for the "store the
bytes, serve the bytes" half of what Cloudinary was doing. Moving to it means
the composite has to stop being a server-side render:

* **Trim, speed, rotation, the colour effect and the original-audio volume**
  are applied by the player, at playback, the same way the editor already
  previewed them — the file on disk is never touched. See `swifts.capture` for
  the settings and for the one place this gets subtle, which is how speed
  interacts with the sixty-second ceiling.
* **The music bed is a second audio stream, not a mix.** Mixing a track into
  the video file is a real encode and there is nowhere left to run it — so the
  player opens the track alongside the video and plays both, exactly as it
  already applies trim and volume. `track`, `music_volume` and
  `music_start_ms` are the parameters that describes; the phone does the rest.
  What this costs, and it is worth stating plainly: the audio is only mixed on
  a device playing it through Approx. A Swift downloaded as a file is the
  silent-or-original-sound recording, because that is what the file is.
* **The text overlay and the stickers are drawn as layers over the video by
  the client**, rather than burned into the file — which is exactly how the editor already
  rendered its own preview, so this is the first time the "preview" and the
  real thing are the same code path.
* **The poster frame is captured on the phone** at export time and uploaded
  as an ordinary image, rather than asked for as a timestamp Cloudinary would
  decode the video to extract.

## What is still here

`video_url`/`thumbnail_url`/`media_id` are untouched on the model, and a row
that already has them (posted under the old pipeline) still needs a way to
resolve to something playable — `playback_url`/`poster_url` below fall back to
them. Nothing new writes them: see `SwiftCreateSerializer`, which accepts
`video`/`thumbnail` file uploads instead.
"""

#: Hard ceiling on a Swift, in milliseconds. Enforced on the client for a
#: decent error and again on the server because the client is not trusted.
#: Sixty seconds is where short-form stops being short and the egress bill
#: stops being incidental.
MAX_DURATION_MS = 60_000
MIN_DURATION_MS = 1_000

#: Ceiling on the raw upload. Generous enough for 60s of 1080p at a phone's
#: default bitrate, tight enough that a mis-set camera cannot post a gigabyte.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def playback_url(swift):
    """The address of this Swift's video.

    The uploaded file when there is one — every Swift posted since the R2
    migration has one. Falls back to the legacy `video_url` for a row that
    predates it, which is the only case that column is ever read for.
    """
    if swift.video:
        return swift.video.url
    return swift.video_url or ''


def poster_url(swift):
    """The still frame shown before playback starts.

    The client-captured `thumbnail` when there is one; the legacy
    `thumbnail_url` otherwise. Neither is derived from `cover_ms` any more —
    that column described a timestamp for Cloudinary to extract a frame from,
    and R2 cannot decode video to do the equivalent.
    """
    if swift.thumbnail:
        return swift.thumbnail.url
    return swift.thumbnail_url or ''
