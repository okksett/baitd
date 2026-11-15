"""Calibrate the automatic-detection signals on a real live stream (Baitd 0.5.0, Task 13).

Not packaged with Baitd. It never calls the AI and never transcribes: it captures one channel (Capture plus
the chat reader plus both signals) for N minutes and logs, one JSON object per line, every chat message's
time and whether it laughs (never its text), every segment's loudness, every closed chat bin, and every
spike. `--replay` reruns the signals over saved logs, with other constants, to tune them offline.

    python tools/live_calibrate.py kick.com/<slug> --minutes 10 --out kick-big.jsonl
    python tools/live_calibrate.py --replay kick-big.jsonl twitch-small.jsonl --set CHAT_RATE_X=4 LOUD_LU=9
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

from baitd.live import signals
from baitd.live.buffer import RingBuffer
from baitd.live.capture import Capture
from baitd.live.channels import Channel, parse_channel
from baitd.live.chat import make_reader

CONSTANTS = ("BIN_S", "BASELINE_S", "MIN_HISTORY_S", "CHAT_RATE_X", "CHAT_MIN_MSGS", "LAUGH_SHARE", "LAUGH_MIN",
             "LAUGH_RATE_X", "LOUD_LU", "COOLDOWN_S")


class Log:
    def __init__(self, path: Path):
        self._fh = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, **row) -> None:
        with self._lock:
            self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def record(url: str, minutes: float, out: Path, max_height: int) -> None:
    platform, handle, canon = parse_channel(url)
    ch = Channel(id="calibrate", platform=platform, handle=handle, url=canon)
    work = Path(tempfile.mkdtemp(prefix="baitd-calibrate-"))
    buf = RingBuffer(work / "ts")
    log = Log(out)
    chat_sig, audio_sig = signals.ChatSignal(), signals.AudioSignal()
    bins: dict[int, list[int]] = {}
    bins_lock = threading.Lock()

    def on_event(kind: str, payload: dict) -> None:
        log.write(type="event", wall=time.time(), kind=kind, text=str((payload or {}).get("text") or ""))
        print(f"[{kind}] {(payload or {}).get('text', '')}", flush=True)

    cap = Capture(ch, buf, max_height, on_event)
    segs: queue.Queue = queue.Queue()
    cap.on_segment = lambda path, start, duration: segs.put((path, start, duration))
    stopping = threading.Event()

    def meter() -> None:
        signals.below_normal_thread()
        while not stopping.is_set():
            try:
                path, start, duration = segs.get(timeout=0.5)
            except queue.Empty:
                continue
            began = time.perf_counter()
            lufs = signals.segment_loudness(path)
            took = time.perf_counter() - began
            log.write(type="seg", t=start, dur=duration, lufs=lufs, wall=time.time(), cost_s=round(took, 3))
            if lufs is not None:
                audio_sig.add(start, lufs)

    def on_message(t: float, text: str) -> None:
        laugh = signals.LAUGH.search(text) is not None
        log.write(type="msg", t=t, laugh=laugh)
        chat_sig.add(t, text)
        with bins_lock:
            b = bins.setdefault(int(t // signals.BIN_S), [0, 0])
            b[0] += 1
            b[1] += laugh

    cap.start()
    deadline = time.time() + minutes * 60
    reader = None
    meter_t = threading.Thread(target=meter, daemon=True)
    meter_t.start()
    last_bin = None
    try:
        while time.time() < deadline and cap.state not in ("ended", "error"):
            time.sleep(1.0)
            if reader is None and cap.state == "capturing":
                reader = make_reader(ch, on_message, on_event, video_id=str(cap.info.get("id") or ""))
                reader.start()
                log.write(type="start", wall=time.time(), platform=platform, handle=handle,
                          title=cap.title)
            now = time.time()
            idx = int(now // signals.BIN_S) - 1
            if last_bin is not None and idx > last_bin:
                with bins_lock:
                    for i in range(last_bin + 1, idx + 1):
                        n, laughs = bins.get(i, (0, 0))
                        log.write(type="bin", t=i * signals.BIN_S, n=n, laughs=laughs)
            last_bin = idx
            for sp in (chat_sig.check(now), audio_sig.check()):
                if sp is not None:
                    log.write(type="spike", t=sp.t, reason=sp.reason, strength=sp.strength, wall=now)
                    print(f"SPIKE {sp.reason} strength={sp.strength} t={sp.t:.0f}", flush=True)
    finally:
        stopping.set()
        if reader is not None:
            reader.stop()
        cap.stop(timeout=10)
        meter_t.join(5)
        buf.clear()
        log.write(type="end", wall=time.time(), state=cap.state)
        log.close()
    print(json.dumps(summary(load(out)), indent=1))


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def replay(rows: list[dict]) -> list[signals.Spike]:
    """Rerun both signals over a recorded log with the module's current constants."""
    chat_sig, audio_sig = signals.ChatSignal(), signals.AudioSignal()
    spikes: list[signals.Spike] = []
    msgs = sorted((r for r in rows if r["type"] == "msg"), key=lambda r: r["t"])
    if msgs:
        t = msgs[0]["t"]
        i = 0
        while t <= msgs[-1]["t"] + signals.BIN_S:
            while i < len(msgs) and msgs[i]["t"] <= t:
                chat_sig.add(msgs[i]["t"], "jaja" if msgs[i]["laugh"] else "hola")
                i += 1
            sp = chat_sig.check(t)
            if sp is not None:
                spikes.append(sp)
            t += 1.0
    for r in sorted((r for r in rows if r["type"] == "seg" and r["lufs"] is not None), key=lambda r: r["t"]):
        audio_sig.add(r["t"], r["lufs"])
        sp = audio_sig.check()
        if sp is not None:
            spikes.append(sp)
    return spikes


def summary(rows: list[dict]) -> dict:
    start = next((r for r in rows if r["type"] == "start"), {})
    msgs = [r for r in rows if r["type"] == "msg"]
    segs = [r for r in rows if r["type"] == "seg"]
    loud = [r["lufs"] for r in segs if r["lufs"] is not None]
    bins = [r["n"] for r in rows if r["type"] == "bin"]
    span = (max(r["t"] for r in msgs) - min(r["t"] for r in msgs)) / 60 if len(msgs) > 1 else 0.0
    spikes = replay(rows)
    by = {k: sum(1 for s in spikes if s.reason == k) for k in ("chat", "laughs", "loud")}
    return {
        "channel": f"{start.get('platform')}/{start.get('handle')}",
        "minutes_of_chat": round(span, 1),
        "messages": len(msgs),
        "msgs_per_min": round(len(msgs) / span, 1) if span else 0,
        "laugh_share": round(sum(r["laugh"] for r in msgs) / len(msgs), 3) if msgs else 0,
        "bin_median": statistics.median(bins) if bins else 0,
        "bin_max": max(bins) if bins else 0,
        "segments": len(segs),
        "lufs_median": round(statistics.median(loud), 1) if loud else None,
        "lufs_p90": round(sorted(loud)[int(len(loud) * 0.9)], 1) if loud else None,
        "lufs_max": round(max(loud), 1) if loud else None,
        "meter_cost_s_median": round(statistics.median(r.get("cost_s", 0) for r in segs), 3) if segs else None,
        "spikes": by,
        "spikes_total": len(spikes),
        "events": [r["kind"] for r in rows if r["type"] == "event"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="channel link (kick.com/x, twitch.tv/x, youtube.com/@x)")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--out", type=Path, default=Path("calibrate.jsonl"))
    ap.add_argument("--max-height", type=int, default=720)
    ap.add_argument("--replay", nargs="+", type=Path, help="recorded logs to rerun instead of capturing")
    ap.add_argument("--set", nargs="*", default=[], metavar="NAME=VALUE", help="override signal constants")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    for item in args.set:
        name, _, value = item.partition("=")
        if name not in CONSTANTS:
            sys.exit(f"unknown constant {name}; one of {', '.join(CONSTANTS)}")
        setattr(signals, name, type(getattr(signals, name))(float(value)))
    if args.replay:
        for path in args.replay:
            print(json.dumps(summary(load(path)), ensure_ascii=False))
        return
    if not args.url:
        ap.error("give a channel link or --replay")
    record(args.url, args.minutes, args.out, args.max_height)


if __name__ == "__main__":
    main()
