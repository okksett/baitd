from datetime import datetime, timezone

from baitd.live import hls

UTC = timezone.utc


def _ts(*args):
    return datetime(*args, tzinfo=UTC).timestamp()


KICK = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DATERANGE:ID="x",START-DATE="2026-11-15T15:47:49.328Z",X-SERVER-TIME="1794757701.19"
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T15:47:49.328Z
#EXTINF:4.167,live
https://x/s100.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T15:47:53.495Z
#EXTINF:4.167,live
https://x/s101.ts
#EXT-X-PREFETCH:https://x/p.ts
"""


def test_kick_playlist():
    pl = hls.parse_media_playlist(KICK, "https://x/index.m3u8")
    assert len(pl.segments) == 2
    assert pl.segments[0].start == _ts(2026, 11, 15, 15, 47, 49, 328000)
    assert pl.segments[1].start == _ts(2026, 11, 15, 15, 47, 53, 495000)
    assert pl.segments[0].seq == 100
    assert pl.segments[1].seq == pl.segments[0].seq + 1
    assert pl.server_time == 1794757701.19
    assert pl.target_duration == 5.0
    assert not any(s.ad for s in pl.segments)
    assert not pl.ended and not pl.has_map


TWITCH = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T10:00:00.000Z
#EXTINF:2.000,Amazon|2474283100495.0
https://t/a.ts
#EXT-X-DATERANGE:ID="ad1",CLASS="twitch-stitched-ad",START-DATE="2026-11-15T10:00:02.000Z",DURATION=4.0
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T10:00:02.000Z
#EXTINF:2.000,live
https://t/b.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T10:00:06.000Z
#EXTINF:2.000,live
https://t/c.ts
"""


def test_twitch_ad_segments():
    pl = hls.parse_media_playlist(TWITCH, "https://t/index.m3u8")
    assert [s.ad for s in pl.segments] == [True, True, False]


YOUTUBE = """#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:50
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T09:00:00.000Z
#EXTINF:5.0,
a.ts
#EXTINF:5.0,
b.ts
#EXTINF:5.0,
c.ts
"""


def test_youtube_playlist_chains_times():
    pl = hls.parse_media_playlist(YOUTUBE, "https://y/index.m3u8")
    t = _ts(2026, 11, 15, 9, 0, 0)
    assert [s.start for s in pl.segments] == [t, t + 5, t + 10]
    assert not any(s.ad for s in pl.segments)


def test_cuepoint_ad_daterange_is_not_an_ad():
    text = YOUTUBE.replace(
        "#EXTINF:5.0,\nb.ts",
        '#EXT-X-DATERANGE:ID="c",CLASS="CUEPOINT-AD",START-DATE="2026-11-15T09:00:05.000Z",DURATION=5.0\n#EXTINF:5.0,\nb.ts',
    )
    pl = hls.parse_media_playlist(text, "https://y/index.m3u8")
    assert not any(s.ad for s in pl.segments)


def test_relative_urls_end_and_map():
    text = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.0,\nseg1.ts\n#EXT-X-ENDLIST\n'
    pl = hls.parse_media_playlist(text, "https://h/a/b/index.m3u8")
    assert pl.segments[0].url == "https://h/a/b/seg1.ts"
    assert pl.segments[0].seq == 0
    assert pl.ended is True
    assert pl.has_map is True


def test_no_program_date_time():
    text = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:3\n#EXTINF:4.0,\na.ts\n#EXTINF:4.0,\nb.ts\n"
    pl = hls.parse_media_playlist(text, "https://h/i.m3u8")
    assert len(pl.segments) == 2
    assert all(s.start is None for s in pl.segments)
    assert pl.server_time is None


def test_is_master():
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nhigh.m3u8\n"
    assert hls.is_master(master) is True
    assert hls.is_master(YOUTUBE) is False


def test_map_url_per_segment():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
        '#EXT-X-MAP:URI="init0.mp4"\n#EXTINF:2.0,\na.m4s\n#EXTINF:2.0,\nb.m4s\n'
        '#EXT-X-MAP:URI="init1.mp4"\n#EXTINF:2.0,\nc.m4s\n'
    )
    pl = hls.parse_media_playlist(text, "https://h/a/index.m3u8")
    assert [s.map_url for s in pl.segments] == [
        "https://h/a/init0.mp4", "https://h/a/init0.mp4", "https://h/a/init1.mp4",
    ]
    plain = hls.parse_media_playlist("#EXTM3U\n#EXTINF:2.0,\na.ts\n", "https://h/a/index.m3u8")
    assert [s.map_url for s in plain.segments] == [None]


def test_other_titles_are_not_ads_on_a_playlist_that_never_says_live():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n" + "".join(
        f"#EXTINF:4.0,Segmento {i}\nhttps://x/s{i}.ts\n" for i in range(3))
    pl = hls.parse_media_playlist(text, "https://x/index.m3u8")
    assert [s.ad for s in pl.segments] == [False, False, False]


def test_twitch_style_titles_still_mark_ads_when_some_segment_is_live():
    text = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXTINF:4.0,live\nhttps://x/s1.ts\n#EXTINF:4.0,Amazon|123\nhttps://x/s2.ts\n")
    pl = hls.parse_media_playlist(text, "https://x/index.m3u8")
    assert [s.ad for s in pl.segments] == [False, True]
