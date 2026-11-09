"""Subtitles from the voiceover itself: local Whisper (faster-whisper) with word timestamps.

Runs on the CPU, nothing leaves the machine. The prompt primes the names Whisper would otherwise
mangle; a small fix-up table catches the rest. Output is a list of (start, end, text) lines in video
time, and an .srt for uploading alongside the video.
"""

from __future__ import annotations

import re
from pathlib import Path

PRIMER = ("Holt, Otari, Mozilla AI, DeepWiki, MCP, GLM 5.3, DeepSeek V4.1 Flash, DeepSeek V4 Pro, holt-writer, "
          "open-weight models, guardrail, prompt injection, maintainers, GitHub, commit d2cd875.")
FIXES = [
    # spacing Whisper leaves around hyphens, decimals and percent signs
    (r"(\w) -(\w)", r"\1-\2"), (r"(\d) \.(\d)", r"\1.\2"), (r"(\d) %", r"\1%"),
    # names and terms of this project
    (r"\b[Oo]t(?:ary|arri|ori|tari|aree)\b", "Otari"), (r"\b[Dd]eep ?[VWvw]iki\b", "DeepWiki"),
    (r"\b[Hh]olt[ -]?writer\b", "holt-writer"), (r"\bG\.?L\.?M\.?(?= |\d|$)", "GLM"), (r"\bGLM ?5 ?(?:country|point three|\.3)\b", "GLM 5.3"),
    (r"\bDeep ?[Ss]eek\b", "DeepSeek"), (r"\bM\.?C\.?P\.?\b", "MCP"), (r"\bopen weight\b", "open-weight"),
    (r"\bcommit [Dd]2 ?[Cc][Dd](?: ?875)?\b", "commit d2cd875"), (r"\bMozilla'?s A\.?I\.?\b", "Mozilla AI's"),
    (r"\b(?:card|guard) will\b", "guardrail"), (r"\b[Ff]or user budgets\b", lambda m: ("Per" if m[0][0] == "F" else "per") + "-user budgets"),
    # words misheard in this script's context
    (r"\bdaily gap\b", "daily cap"), (r"\btears the writer\b", "tiers the Writer"), (r"\bwhat the run caused\b", "what the run cost"),
    (r"\brepo's onset\b", "repo's own set"), (r"\bKind severity labels\b", "Kind, severity, labels"),
    (r"\breport is returned from\b", "report is written from"), (r"\ban attacker\. This\b", "an attack. This"),
    (r"\bstranger's text\b", "strangers' text"),
]
MAX_CHARS = 46
GAP_BREAK = 0.55


def transcribe(audio: Path, model: str = "small.en") -> list[tuple[float, float, str]]:
    from faster_whisper import WhisperModel

    import subprocess

    import imageio_ffmpeg
    import numpy as np

    # Decode with our own ffmpeg (16 kHz mono floats) rather than faster-whisper's PyAV path, whose
    # keyword arguments drift between PyAV releases.
    raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(audio), "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    m = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _ = m.transcribe(samples, language="en", word_timestamps=True, vad_filter=True,
                               initial_prompt=PRIMER, condition_on_previous_text=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append((float(w.start), float(w.end), text))
    return words


def fix(text: str, names: tuple[tuple[str, str], ...] = ()) -> str:
    for pat, rep in FIXES:
        text = re.sub(pat, rep, text)
    for heard, meant in names:
        text = re.sub(rf"\b{re.escape(heard)}\b", meant, text)
    return text


NAMES: tuple[tuple[str, str], ...] = (("Sanjay", "Sanchay"),)  # how Whisper hears the presenter's name


def fix_words(words: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Apply FIXES across runs of up to four words, before lines are cut, so a fix can't be split by a line break."""
    words = [tuple(w) for w in words]
    out, i = [], 0
    while i < len(words):
        for n in (4, 3, 2, 1):
            chunk = words[i:i + n]
            if len(chunk) < n:
                continue
            phrase = " ".join(w[2] for w in chunk)
            fixed = fix(phrase, NAMES)
            if fixed != phrase:
                tokens = fixed.split()
                start, end = chunk[0][0], chunk[-1][1]
                step = (end - start) / max(1, len(tokens))
                out.extend((start + k * step, start + (k + 1) * step, tok) for k, tok in enumerate(tokens))
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def lines(words: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Group words into short subtitle lines, breaking on pauses, sentence ends and length."""
    words = fix_words(words)
    out, cur = [], []
    for i, (s, e, w) in enumerate(words):
        if cur:
            text = " ".join(x[2] for x in cur)
            gap = s - cur[-1][1]
            if gap > GAP_BREAK or len(text) + 1 + len(w) > MAX_CHARS or re.search(r"[.!?]$", cur[-1][2]) and len(text) > 18:
                out.append(cur)
                cur = []
        cur.append((s, e, w))
    if cur:
        out.append(cur)
    result = []
    for k, grp in enumerate(out):
        start, end = grp[0][0], grp[-1][1] + 0.25
        if k + 1 < len(out):
            end = min(end, out[k + 1][0][0] - 0.05)
        end = max(end, start + 0.9)
        result.append((round(start, 2), round(end, 2), fix(" ".join(x[2] for x in grp), NAMES)))
    return result


def srt(subs: list[tuple[float, float, str]]) -> str:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i}\n{ts(s)} --> {ts(e)}\n{text}\n" for i, (s, e, text) in enumerate(subs, 1))
