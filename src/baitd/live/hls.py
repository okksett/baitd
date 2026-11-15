"""Parser for HLS media playlists, with ad and wall-clock detection."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


@dataclass(frozen=True)
class Segment:
    seq: int
    url: str
    duration: float
    start: float | None  # epoch seconds
    ad: bool
    map_url: str | None = None  # init segment (EXT-X-MAP) in effect for this segment


@dataclass
class MediaPlaylist:
    segments: list[Segment]
    target_duration: float
    ended: bool
    server_time: float | None
    has_map: bool


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def _attrs(line: str) -> dict[str, str]:
    body = line.split(":", 1)[1] if ":" in line else ""
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(body)}


def _epoch(value: str) -> float | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    seq = 0
    target = 0.0
    ended = False
    has_map = False
    server_time: float | None = None
    ad_ranges: list[tuple[float, float]] = []
    # (seq, url, duration, title, pdt, map_url) per segment, in playlist order
    raw: list[tuple[int, str, float, str, float | None, str | None]] = []
    cur_map: str | None = None

    pending_dur = 0.0
    pending_title = ""
    pending_pdt: float | None = None
    have_extinf = False

    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                seq = int(_float(line.split(":", 1)[1]) or 0)
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                target = _float(line.split(":", 1)[1]) or 0.0
            elif line.startswith("#EXT-X-ENDLIST"):
                ended = True
            elif line.startswith("#EXT-X-MAP"):
                has_map = True
                uri = _attrs(line).get("URI")
                cur_map = urljoin(base_url, uri) if uri else cur_map
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pending_pdt = _epoch(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-DATERANGE"):
                attrs = _attrs(line)
                st = _float(attrs.get("X-SERVER-TIME"))
                if st is not None and (server_time is None or st > server_time):
                    server_time = st
                if attrs.get("CLASS") == "twitch-stitched-ad":
                    begin = _epoch(attrs.get("START-DATE", ""))
                    dur = _float(attrs.get("DURATION"))
                    if begin is not None and dur is not None:
                        ad_ranges.append((begin, begin + dur))
            elif line.startswith("#EXTINF:"):
                head, _, title = line.split(":", 1)[1].partition(",")
                pending_dur = _float(head) or 0.0
                pending_title = title.strip()
                have_extinf = True
            continue
        # a URI line; EXT-X-PREFETCH lines start with '#', so they never get here
        if have_extinf:
            raw.append((seq + len(raw), urljoin(base_url, line), pending_dur, pending_title, pending_pdt, cur_map))
        have_extinf = False
        pending_pdt = None
        pending_title = ""
        pending_dur = 0.0

    segments: list[Segment] = []
    prev_start: float | None = None
    prev_dur = 0.0
    for s_seq, url, dur, title, pdt, map_url in raw:
        if pdt is not None:
            start: float | None = pdt
        elif prev_start is not None:
            start = prev_start + prev_dur
        else:
            start = None
        ad = bool(title) and not title.lower().startswith("live")
        if not ad and start is not None:
            ad = any(a <= start < b for a, b in ad_ranges)
        segments.append(Segment(s_seq, url, dur, start, ad, map_url))
        prev_start, prev_dur = start, dur

    return MediaPlaylist(segments, target, ended, server_time, has_map)
