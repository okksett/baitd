"""Who is talking, by voice: split the sentences around a clip into two voices.

Each sentence gets the mean and spread of its MFCCs (the timbre of the voice, loudness left out); two-means
clustering then puts each sentence in one of two groups. On a podcast where one guest eats while talking,
mouth motion picked the right person in 8 of 16 sentences; these groups matched the conversation in 16 of 16.
The groups only say "same voice / other voice": framing.py decides which face each voice belongs to.
"""
from __future__ import annotations

import subprocess
import threading

import numpy as np

from .. import ffmpeg
from ..ffmpeg import JobCancelled

RATE = 16000
N_FFT, HOP, N_MELS, N_MFCC = 512, 160, 40, 20
MIN_SENTENCE_S = 0.8      # shorter sentences carry too little voice to place
MIN_SENTENCES = 4         # fewer than this: no groups
# Measured on real footage: a 2-minute monologue split off 1 sentence in 18 (share 0.06, silhouette 0.28);
# a two-person conversation split 7/9 (share 0.44) with a silhouette of only 0.09, all 16 sentences right.
# So the share decides; the silhouette only rejects groups that are no groups at all.
MIN_SHARE = 0.2           # each voice needs at least this share of the sentences ...
MIN_SEPARATION = 0.05     # ... and the groups must be at least this distinct (mean silhouette)
_RESTARTS = 20


def load_audio(path, start: float, end: float, cancel: threading.Event | None = None) -> np.ndarray:
    """[start, end) of the file's audio as 16 kHz mono float32."""
    if cancel is not None and cancel.is_set():
        raise JobCancelled()
    out = subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}",
         "-to", f"{end:.3f}", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        capture_output=True, stdin=subprocess.DEVNULL, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768


def _filters() -> tuple[np.ndarray, np.ndarray]:
    lo, hi = 2595 * np.log10(1 + 60 / 700), 2595 * np.log10(1 + 7600 / 700)
    hz = 700 * (10 ** (np.linspace(lo, hi, N_MELS + 2) / 2595) - 1)
    bins = np.floor((N_FFT + 1) * hz / RATE).astype(int)
    bank = np.zeros((N_MELS, N_FFT // 2 + 1))
    for m in range(1, N_MELS + 1):
        a, b, c = bins[m - 1], bins[m], bins[m + 1]
        bank[m - 1, a:b] = (np.arange(a, b) - a) / max(1, b - a)
        bank[m - 1, b:c] = (c - np.arange(b, c)) / max(1, c - b)
    k = np.arange(N_MELS)
    dct = np.cos(np.pi / N_MELS * (k + 0.5)[None, :] * np.arange(N_MFCC)[:, None])
    return bank, dct


_BANK, _DCT = _filters()


def voiceprint(samples: np.ndarray) -> np.ndarray | None:
    """Mean and spread of the MFCCs (without c0, the loudness) over the louder frames; None if too short."""
    if len(samples) < MIN_SENTENCE_S * RATE:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(samples, N_FFT)[::HOP] * np.hanning(N_FFT)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    energy = power.sum(1)
    keep = energy > np.percentile(energy, 40)  # the voice, not the pauses between words
    if keep.sum() < 10:
        return None
    feats = (_DCT @ np.log(_BANK @ power[keep].T + 1e-10)).T[:, 1:]
    return np.concatenate([feats.mean(0), feats.std(0)])


def _two_means(x: np.ndarray) -> np.ndarray:
    rng = np.random.default_rng(0)  # deterministic: the same clip always gets the same groups
    best_cost, best = None, None
    for _ in range(_RESTARTS):
        centres = x[rng.choice(len(x), 2, replace=False)]
        for _ in range(50):
            labels = np.argmin(((x[:, None] - centres[None]) ** 2).sum(2), 1)
            new = np.array([x[labels == q].mean(0) if (labels == q).any() else centres[q] for q in range(2)])
            if np.allclose(new, centres):
                break
            centres = new
        cost = ((x - centres[labels]) ** 2).sum()
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, labels
    return best


def _silhouette(x: np.ndarray, labels: np.ndarray) -> float:
    d = np.sqrt(((x[:, None] - x[None]) ** 2).sum(2))
    scores = []
    for i in range(len(x)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            scores.append(0.0)
            continue
        a, b = d[i, same].mean(), d[i, labels != labels[i]].mean()
        scores.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(scores))


def separate(prints: list[np.ndarray | None]) -> tuple[list[int | None], float]:
    """Group the voiceprints into two voices: (0, 1 or None per sentence, separation). All None when the
    sentences do not split into two clear voices (one person talking, or too little to go on)."""
    idx = [i for i, p in enumerate(prints) if p is not None]
    labels: list[int | None] = [None] * len(prints)
    if len(idx) < MIN_SENTENCES:
        return labels, 0.0
    x = np.array([prints[i] for i in idx])
    x = (x - x.mean(0)) / (x.std(0) + 1e-9)
    groups = _two_means(x)
    share = min(np.mean(groups == 0), np.mean(groups == 1))
    separation = _silhouette(x, groups)
    if share < MIN_SHARE or separation < MIN_SEPARATION:
        return labels, separation
    for i, g in zip(idx, groups):
        labels[i] = int(g)
    return labels, separation


def sentence_voices(path, spans: list[tuple[float, float]], cancel: threading.Event | None = None
                    ) -> tuple[list[int | None], float]:
    """Voices of `spans` (absolute seconds in the file at `path`): see separate()."""
    if len(spans) < MIN_SENTENCES:
        return [None] * len(spans), 0.0
    start, end = min(s for s, _ in spans), max(e for _, e in spans)
    audio = load_audio(path, start, end, cancel)
    prints = []
    for s0, s1 in spans:
        if cancel is not None and cancel.is_set():
            raise JobCancelled()
        prints.append(voiceprint(audio[int((s0 - start) * RATE):int((s1 - start) * RATE)]))
    return separate(prints)
