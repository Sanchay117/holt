"""Teleprompter for Holt's voiceover, and the step that lines your recording back up.

The audio comes first. build_cues() turns the script into timed slots (each scene gets as long as it
takes to read at a relaxed pace, or as long as its footage needs, whichever is longer). render() makes
a 1080p teleprompter: the current scene's lines in large type, highlighted word by word at that pace,
what's next, and a 5-second "3, 2, 1, go" lead-in with beeps. Play it full screen and record yourself
with the mic on (Win+Alt+R records the screen plus both audio sources).

The recorder then shoots the footage to the same slots, and align_and_mux() finds the "go" beep in your
recording, trims everything before it, tidies the audio (high-pass, gentle noise reduction, loudness
normalisation; plain DSP, no AI) and lays it on the footage.
"""

from __future__ import annotations

import math
import re
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

FPS = 5
W, H = 1920, 1080
TEXT_X, TEXT_W, TEXT_TOP, TEXT_BOTTOM = 150, 1620, 220, 900
LINE_H = 92
WORDS_PER_SEC = 2.6
TICK_HZ, GO_HZ = 880, 1320

C = {
    "bg": (9, 10, 13), "line": (40, 43, 52), "text": (242, 242, 246), "read": (88, 92, 104), "muted": (146, 149, 161),
    "faint": (104, 107, 119), "accent": (244, 184, 96), "accent2": (255, 138, 92), "ok": (95, 212, 155), "warn": (242, 201, 76),
}
FONT_DIRS = (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts"))
SANS = ("segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf")
SEMI = ("seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf")
MONO = ("consola.ttf", "DejaVuSansMono.ttf", "Menlo.ttc")


def _font(size: int, names: tuple[str, ...]) -> ImageFont.FreeTypeFont:
    for name in names:
        for base in FONT_DIRS:
            if (base / name).exists():
                return ImageFont.truetype(str(base / name), size)
    return ImageFont.load_default(size)


@dataclass
class Cue:
    key: str
    title: str
    start: float
    end: float
    text: str
    hint: str = ""  # what's on screen, for the reader
    words: list[str] = field(default_factory=list)
    times: list[float] = field(default_factory=list)  # when each word is spoken
    speech_end: float = 0.0

    def plan(self) -> None:
        """Spread the words over the slot at a natural pace; longer words and full stops take longer."""
        self.words = self.text.split()
        self.times = []
        if not self.words:
            self.speech_end = self.start
            return
        weights = [len(w) + 3 + (6 if re.search(r"[.!?:]$", w) else 3 if w.endswith(",") else 0) for w in self.words]
        span = min(len(self.words) / WORDS_PER_SEC, max(0.5, self.end - self.start - 0.8))
        t, total = self.start + 0.4, sum(weights)
        for w in weights:
            self.times.append(t)
            t += span * w / total
        self.speech_end = t

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "start": round(self.start, 2), "end": round(self.end, 2),
                "text": self.text, "hint": self.hint}


def build_cues(script: list[dict], min_seconds: dict[str, float]) -> list[Cue]:
    """script: [{key, title, text, hint}] -> back-to-back slots, each long enough to read and to film."""
    cues, t = [], 0.0
    for s in script:
        need = len(s["text"].split()) / WORDS_PER_SEC + 1.8
        dur = max(need, min_seconds.get(s["key"], 0))
        cues.append(Cue(s["key"], s["title"], t, t + dur, s["text"], s.get("hint", "")))
        t += dur
    for c in cues:
        c.plan()
    return cues


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def media_duration(path: Path) -> float:
    out = subprocess.run([ffmpeg(), "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    if not m:
        raise RuntimeError(f"Couldn't read the duration of {path}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _wrap(draw: ImageDraw.ImageDraw, words: list[str], font, width: int) -> list[list[tuple[int, str, int]]]:
    space = draw.textlength(" ", font=font)
    lines, line, x = [], [], 0.0
    for i, w in enumerate(words):
        wl = draw.textlength(w, font=font)
        if line and x + wl > width:
            lines.append(line)
            line, x = [], 0.0
        line.append((i, w, int(x)))
        x += wl + space
    if line:
        lines.append(line)
    return lines


class Renderer:
    def __init__(self, cues: list[Cue], lead_in: float):
        self.cues, self.lead = cues, lead_in
        self.total = cues[-1].end if cues else 0.0
        self.f_body = _font(62, SANS)
        self.f_title = _font(34, SEMI)
        self.f_small = _font(21, SEMI)
        self.f_hint = _font(28, SANS)
        self.f_timer = _font(40, MONO)
        self.f_next = _font(30, SANS)
        self.f_huge = _font(260, SEMI)
        scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        self.layouts = [_wrap(scratch, c.words, self.f_body, TEXT_W) for c in cues]

    def _centered(self, d, y, text, font, fill):
        d.text(((W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    def frame(self, t: float) -> Image.Image:
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        if t < 0:
            n = math.ceil(-t)
            self._centered(d, 250, str(n), self.f_huge, C["accent"])
            self._centered(d, 600, "Start reading when it hits 0:00.", self.f_title, C["text"])
            self._centered(d, 660, "Follow the highlighted word: it moves at the pace the footage will be cut to.", self.f_hint, C["muted"])
            self._centered(d, 706, "Your own words are fine. Pauses between scenes are built in.", self.f_hint, C["muted"])
            self._centered(d, 790, "Record with the mic on and keep the beeps in (Win + Alt + R records both).", self.f_hint, C["faint"])
            return img
        i = next((k for k, c in enumerate(self.cues) if c.start <= t < c.end), len(self.cues) - 1)
        cue = self.cues[i]

        d.text((TEXT_X, 58), "HOLT  ·  VOICEOVER", font=self.f_small, fill=C["muted"])
        d.text((TEXT_X, 94), f"{i + 1}/{len(self.cues)}   {cue.title}", font=self.f_title, fill=C["text"])
        timer = f"{mmss(t)} / {mmss(self.total)}"
        d.text((W - TEXT_X - d.textlength(timer, font=self.f_timer), 92), timer, font=self.f_timer, fill=C["text"])
        span = max(0.1, cue.end - cue.start)
        d.rounded_rectangle((TEXT_X, 160, W - TEXT_X, 164), 2, fill=C["line"])
        d.rounded_rectangle((TEXT_X, 160, TEXT_X + int((W - 2 * TEXT_X) * min(1.0, (t - cue.start) / span)), 164), 2, fill=C["accent"])

        current = sum(1 for wt in cue.times if wt <= t) - 1
        lines = self.layouts[i]
        max_lines = (TEXT_BOTTOM - TEXT_TOP) // LINE_H
        cur_line = next((n for n, ln in enumerate(lines) if any(idx == current for idx, _, _ in ln)), 0)
        first = 0 if len(lines) <= max_lines else max(0, min(cur_line - 2, len(lines) - max_lines))
        for n, ln in enumerate(lines[first:first + max_lines]):
            y = TEXT_TOP + n * LINE_H
            for idx, word, x in ln:
                color = C["text"] if idx > current else (C["accent"] if idx == current else C["read"])
                d.text((TEXT_X + x, y), word, font=self.f_body, fill=color)
                if idx == current:
                    d.rounded_rectangle((TEXT_X + x, y + 78, TEXT_X + x + d.textlength(word, font=self.f_body), y + 83), 2, fill=C["accent"])

        if cue.hint:
            d.text((TEXT_X, 922), f"On screen: {cue.hint}", font=self.f_hint, fill=C["faint"])
        if cue.words and t > cue.speech_end + 0.4:
            d.text((TEXT_X, 966), "✓ Done. Pause until the next scene.", font=self.f_hint, fill=C["ok"])
        if i + 1 < len(self.cues):
            nxt = self.cues[i + 1]
            left = nxt.start - t
            label = f"Next: {nxt.title}  ·  in {left:0.1f}s" if left < 10 else f"Next: {nxt.title}  ·  in {mmss(left)}"
            d.text((W - TEXT_X - d.textlength(label, font=self.f_next), 964), label, font=self.f_next,
                   fill=C["accent"] if left < 3 else C["muted"])
        d.rectangle((0, H - 6, W, H), fill=C["line"])
        d.rectangle((0, H - 6, int(W * t / max(self.total, 0.1)), H), fill=C["accent2"])
        return img


def _beeps(path: Path, lead_in: float, rate: int = 48000) -> None:
    """Silence with three ticks before 0:00 and a higher 'go' beep at exactly 0:00."""
    n = int((lead_in + 1.0) * rate)
    samples = [0] * n
    for at, hz, dur in [(lead_in - 3, TICK_HZ, 0.12), (lead_in - 2, TICK_HZ, 0.12), (lead_in - 1, TICK_HZ, 0.12), (lead_in, GO_HZ, 0.25)]:
        s0 = int(at * rate)
        for k in range(int(dur * rate)):
            env = min(1.0, k / 240, (dur * rate - k) / 240)
            samples[s0 + k] = int(0.5 * 32767 * env * math.sin(2 * math.pi * hz * k / rate))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *samples))


def render(cues: list[Cue], out: Path, *, lead_in: float = 5.0) -> Path:
    work = out.parent / "prompter-frames"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    r = Renderer(cues, lead_in)
    for k in range(math.ceil((lead_in + r.total) * FPS)):
        r.frame(k / FPS - lead_in).save(work / f"{k:06d}.png", compress_level=1)
    _beeps(work / "beeps.wav", lead_in)
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(work / "%06d.png"),
                    "-i", str(work / "beeps.wav"), "-filter_complex", "[0:v]fps=30,format=yuv420p[v];[1:a]apad[a]",
                    "-map", "[v]", "-map", "[a]", "-t", f"{lead_in + r.total:.2f}", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "20", "-color_range", "tv", "-colorspace", "bt709", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", str(out)], check=True)
    shutil.rmtree(work, ignore_errors=True)
    return out


# ------------------------------------------------------------------- aligning

def _pcm(path: Path, seconds: float, rate: int) -> list[int]:
    raw = subprocess.run([ffmpeg(), "-loglevel", "error", "-i", str(path), "-t", str(seconds), "-vn", "-ac", "1", "-ar", str(rate),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


def _tone(window: list[int], hz: float, rate: int) -> float:
    """Share of the window's energy at one frequency (Goertzel), 0..1."""
    coeff = 2 * math.cos(2 * math.pi * hz / rate)
    s1 = s2 = energy = 0.0
    for x in window:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
        energy += x * x
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return power / (energy * len(window) / 2) if energy else 0.0


def find_go(path: Path, search: float = 120.0) -> float | None:
    """Seconds into a recording where the teleprompter's 'go' beep starts, or None."""
    rate, win = 16000, 320  # 20 ms windows
    pcm = _pcm(path, search, rate)
    loud = max((abs(v) for v in pcm), default=0)
    if not loud:
        return None
    for k in range(0, len(pcm) - win * 4, win):
        w = pcm[k:k + win]
        if max(abs(v) for v in w) < loud * 0.05:
            continue
        if _tone(w, GO_HZ, rate) > 0.45 and _tone(pcm[k + win:k + 2 * win], GO_HZ, rate) > 0.45:
            # step back to the onset within this window
            return max(0.0, (k - win // 2) / rate)
    return None


TARGET_LUFS = -14.0  # YouTube's reference loudness


def _loudness(path: Path, chain: str) -> dict:
    """Measure what loudnorm sees after `chain` (ffmpeg's EBU R128 analysis)."""
    import json
    err = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path), "-vn", "-af",
                          f"{chain},loudnorm=I={TARGET_LUFS}:TP=-1.0:LRA=9:print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return json.loads(err[err.rindex("{"):err.rindex("}") + 1])


def voice_chain(voice: Path, start: float) -> str:
    """Clean the voice, bring it up to speech level, even it out, then normalise it exactly (two passes)."""
    trim = f"atrim=start={start:.3f},asetpts=PTS-STARTPTS"
    clean = "highpass=f=80,lowpass=f=13000,afftdn=nr=14:nf=-60:tn=1"  # before any gain, so the hiss isn't boosted first
    raw = _loudness(voice, f"{trim},{clean}")
    gain = max(0.0, min(30.0, -22.0 - float(raw["input_i"])))  # to roughly -22 LUFS, where the compressor can work
    pre = f"{trim},{clean},volume={gain:.1f}dB,acompressor=threshold=-24dB:ratio=3:attack=8:release=220:makeup=2"
    m = _loudness(voice, pre)
    norm = (f"loudnorm=I={TARGET_LUFS}:TP=-1.0:LRA=9:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    return f"{pre},{norm},alimiter=limit=0.891:level=false"


def prepare_voice(voice: Path, out_wav: Path, *, go: float) -> Path:
    """The cleaned, levelled voice on the video's timeline: its 0:00 is the footage's 0:00."""
    start = go + 0.3  # skip the beep itself
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(voice), "-af", f"{voice_chain(voice, start)},adelay=300|300",
                    "-ar", "48000", "-ac", "1", str(out_wav)], check=True)
    return out_wav


def mux(video: Path, wav: Path, out: Path) -> Path:
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(video), "-i", str(wav), "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{media_duration(video):.2f}",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


def align_and_mux(video: Path, voice: Path, out: Path, *, go: float) -> Path:
    wav = prepare_voice(voice, out.with_suffix(".voice.wav"), go=go)
    return mux(video, wav, out)
