"""Motion-graphics pass over the raw screen recording.

The recording stays the subject: it is framed in a rounded window on a gradient backdrop, and the
camera eases in on whatever the cursor is showing, with a caption. Around it: chapter titles, a
tracker of the rubric's Otari features that light up as each one appears on screen, a name lower-
third at the start, an end card, and a progress bar. Nothing here moves in time, so a voiceover cut
to the raw recording stays in sync.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OW, OH, FPS = 1920, 1080, 30
WX, WY, WW, WH, RADIUS = 160, 100, 1600, 900, 22  # the window the recording sits in; subtitles go below it
EASE_S = 0.6

INK = (7, 8, 10)
TEXT = (240, 240, 245)
MUTED = (150, 153, 164)
FAINT = (98, 101, 113)
ACCENT = (244, 184, 96)
ACCENT2 = (255, 138, 92)
OK = (95, 212, 155)

FEATURES = [  # key, chip label: the rubric's Otari features, plus open-weight models
    ("open_weight", "Open-weight"), ("roles", "Distinct roles"), ("routing", "Routing"), ("guardrails", "Guardrails"),
    ("mcp", "MCP"), ("web", "Web search"), ("code", "Code exec"), ("budgets", "Budgets"), ("per_user", "Per-user"),
]

CHAPTER = {  # short chapter titles that fit beside the rubric tracker
    "intro": "Intro", "handle": "Your own budget", "triage": "Live triage", "walkthrough": "What Otari did",
    "report": "The report", "attack": "Prompt-injection attack", "fallback": "Fallback drill", "budget": "Per-user budget",
    "tierdown": "Budget past 80%", "refusal": "Budget spent", "doctor": "Setup check", "outro": "That's Holt",
}

FONT_DIRS = (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts"))


def font(size: int, *names: str) -> ImageFont.FreeTypeFont:
    for name in names or ("seguisb.ttf",):
        for base in FONT_DIRS:
            if (base / name).exists():
                return ImageFont.truetype(str(base / name), size)
    return ImageFont.load_default(size)


SEMI = ("seguisb.ttf", "DejaVuSans-Bold.ttf")
BOLD = ("segoeuib.ttf", "DejaVuSans-Bold.ttf")
REG = ("segoeui.ttf", "DejaVuSans.ttf")
MONO = ("consola.ttf", "DejaVuSansMono.ttf")


def ease(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


def fade(t: float, start: float, end: float, d: float = 0.35) -> float:
    """0..1 envelope: fades in after start, out before end."""
    if t < start or t > end:
        return 0.0
    return ease(min((t - start) / d, (end - t) / d, 1.0))


def with_alpha(img: Image.Image, a: float) -> Image.Image:
    if a >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * a)))
    return out


def pill(text: str, f, *, fg=TEXT, bg=(12, 13, 17, 228), dot=ACCENT, pad=(22, 13), outline=(255, 255, 255, 30)) -> Image.Image:
    scratch = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    tw = scratch.textlength(text, font=f)
    asc, desc = f.getmetrics()
    dot_w = 22 if dot else 0
    w, h = int(tw + pad[0] * 2 + dot_w), asc + desc + pad[1] * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=bg, outline=outline, width=1)
    if dot:
        r = 5
        d.ellipse((pad[0], h // 2 - r, pad[0] + 2 * r, h // 2 + r), fill=dot)
    d.text((pad[0] + dot_w, pad[1] - 1), text, font=f, fill=fg)
    return img


@dataclass
class Focus:
    t: float
    end: float
    cx: float
    cy: float
    z: float
    caption: str
    caption_img: Image.Image | None


class Compositor:
    def __init__(self, timeline: dict, src_size: tuple[int, int], total: float, presenter: str, repo: str,
                 subtitles: list[tuple[float, float, str]] | None = None):
        self.sw, self.sh = src_size
        self.scale = self.sw / 1280  # CSS px -> source px (the page was captured at a 1280-wide viewport)
        self.total = total
        self.scenes = timeline["scenes"]
        self.presenter = presenter
        self.repo = repo
        self.bg = self._backdrop()
        self.bg_clean = self._backdrop(shadow=False)
        self.mask = self._mask(WW, WH, RADIUS)
        self.f_chapter = font(30, *SEMI)
        self.f_num = font(26, *MONO)
        self.f_chip = font(17, *SEMI)
        self.f_caption = font(27, *SEMI)
        self.lit = self._first_lit(timeline.get("features", []))
        self.chips = self._chips()
        room = self.chips[0][0] - WX - 24  # never run into the tracker
        self.chapters = [c.crop((0, 0, min(c.width, room), c.height)) for c in (self._chapter_img(i, s) for i, s in enumerate(self.scenes))]
        self.focus = self._focus(timeline.get("focus", []))
        self.transitions: list[tuple[float, tuple, tuple]] = []
        for fx in self.focus:
            target = (fx.cx, fx.cy, fx.z) if fx.z > 1.0 else self._home()
            self.transitions.append((fx.t, self.state(fx.t), target))
        outro = next((s for s in self.scenes if s["key"] == "outro"), None)
        self.end_start = (outro["start"] + 1.6) if outro else max(0.0, total - 8)
        self.end_card = self._end_card()
        self.lower_third = self._lower_third()
        f_sub = font(33, *SEMI)
        self.subs = [(s, e, pill(text, f_sub, bg=(8, 9, 12, 210), dot=None, pad=(24, 9), outline=(255, 255, 255, 18)))
                     for s, e, text in (subtitles or [])]

    # ---------------------------------------------------------------- statics

    def _backdrop(self, shadow: bool = True) -> Image.Image:
        img = Image.new("RGB", (OW, OH), INK)
        glow = Image.new("RGB", (OW, OH), (0, 0, 0))
        g = ImageDraw.Draw(glow)
        g.ellipse((-500, -650, 1100, 450), fill=(120, 80, 30))
        g.ellipse((1100, 550, 2500, 1650), fill=(40, 52, 110))
        glow = glow.filter(ImageFilter.GaussianBlur(220))
        img = Image.blend(img, glow, 0.55)
        d = ImageDraw.Draw(img, "RGBA")
        for x in range(0, OW, 64):
            d.line((x, 0, x, OH), fill=(255, 255, 255, 7))
        for y in range(0, OH, 64):
            d.line((0, y, OW, y), fill=(255, 255, 255, 7))
        if shadow:
            sh = Image.new("L", (OW, OH), 0)
            ImageDraw.Draw(sh).rounded_rectangle((WX + 10, WY + 28, WX + WW - 10, WY + WH + 30), RADIUS, fill=170)
            img.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(34)))
        return img

    @staticmethod
    def _mask(w: int, h: int, r: int) -> Image.Image:
        big = Image.new("L", (w * 3, h * 3), 0)
        ImageDraw.Draw(big).rounded_rectangle((0, 0, w * 3 - 1, h * 3 - 1), r * 3, fill=255)
        return big.resize((w, h), Image.LANCZOS)

    def _chapter_img(self, i: int, scene: dict) -> Image.Image:
        num, title = f"{i + 1:02d}", CHAPTER.get(scene["key"], scene["title"])
        scratch = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        w = int(scratch.textlength(num, font=self.f_num) + 18 + scratch.textlength(title, font=self.f_chapter)) + 4
        img = Image.new("RGBA", (w, 46), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((0, 9), num, font=self.f_num, fill=ACCENT)
        d.text((scratch.textlength(num, font=self.f_num) + 18, 2), title, font=self.f_chapter, fill=TEXT)
        return img

    def _first_lit(self, events: list[dict]) -> dict[str, float]:
        lit: dict[str, float] = {}
        for e in sorted(events, key=lambda e: e["t"]):
            for k in e["features"]:
                lit.setdefault(k, e["t"])
        return lit

    def _chips(self) -> list[tuple[int, Image.Image, Image.Image, str]]:
        imgs = []
        for key, label in FEATURES:
            off = pill(label, self.f_chip, fg=FAINT, bg=(255, 255, 255, 10), dot=(70, 73, 84), pad=(13, 7), outline=(255, 255, 255, 26))
            on = pill(label, self.f_chip, fg=(28, 20, 8), bg=ACCENT + (255,), dot=(28, 20, 8), pad=(13, 7), outline=ACCENT2 + (255,))
            imgs.append((key, off, on))
        total_w = sum(o.width for _, o, _ in imgs) + 8 * (len(imgs) - 1)
        x = WX + WW - total_w
        out = []
        for key, off, on in imgs:
            out.append((x, off, on, key))
            x += off.width + 8
        return out

    def _home(self) -> tuple[float, float, float]:
        return (self.sw / 2, self.sh / 2, 1.0)

    def _focus(self, events: list[dict]) -> list[Focus]:
        events = sorted(events, key=lambda e: e["t"])
        out = []
        for i, e in enumerate(events):
            end = events[i + 1]["t"] if i + 1 < len(events) else e["t"] + 6
            end = min(end, e["t"] + 9.0)
            if not e.get("box"):
                out.append(Focus(e["t"], end, self.sw / 2, self.sh / 2, 1.0, "", None))
                continue
            b = e["box"]
            bw, bh = max(40.0, b["width"] * self.scale), max(30.0, b["height"] * self.scale)
            z = min(e.get("zoom") or 1.0, 0.62 * self.sw / bw, 0.80 * self.sh / bh)
            z = max(1.0, z)
            cx, cy = (b["x"] + b["width"] / 2) * self.scale, (b["y"] + b["height"] / 2) * self.scale
            cap = e.get("caption") or ""
            out.append(Focus(e["t"], end, cx, cy, z, cap, pill(cap, self.f_caption) if cap else None))
        return out

    def _lower_third(self) -> Image.Image:
        name_f, sub_f = font(38, *BOLD), font(23, *REG)
        sub = "Holt  ·  MozFest Developers Wilding: Online"
        scratch = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        w = int(max(scratch.textlength(self.presenter, font=name_f), scratch.textlength(sub, font=sub_f))) + 64
        img = Image.new("RGBA", (w, 112), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w - 1, 111), 18, fill=(12, 13, 17, 232), outline=(255, 255, 255, 30))
        d.rounded_rectangle((18, 22, 23, 90), 3, fill=ACCENT)
        d.text((40, 16), self.presenter, font=name_f, fill=TEXT)
        d.text((40, 66), sub, font=sub_f, fill=MUTED)
        return img

    def _end_card(self) -> Image.Image:
        img = Image.new("RGBA", (OW, OH), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx = OW // 2
        # logo: gradient tile with the arc
        tile = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
        grad = Image.new("RGBA", (120, 120))
        gd = ImageDraw.Draw(grad)
        for y in range(120):
            k = y / 119
            gd.line((0, y, 119, y), fill=tuple(int(ACCENT[i] * (1 - k) + ACCENT2[i] * k) for i in range(3)) + (255,))
        m = self._mask(120, 120, 32)
        tile.paste(grad, (0, 0), m)
        ImageDraw.Draw(tile).arc((26, 40, 94, 120), 200, 340, fill=(27, 18, 6), width=10)
        img.alpha_composite(tile, (cx - 60, 250))
        for text, f, color, y in [
            ("Holt", font(104, *BOLD), TEXT, 392),
            ("Issue triage that resists prompt injection", font(38, *SEMI), TEXT, 530),
            ("Open-weight models in four roles  ·  every Otari feature in the rubric  ·  per-maintainer budgets", font(25, *REG), MUTED, 600),
            (self.repo or "Code on GitHub  ·  branch mozfest-devs-wilding  ·  Otari since commit d2cd875", font(25, *MONO), ACCENT, 690),
            ("Built on Otari, Mozilla AI's open-source AI gateway", font(23, *REG), FAINT, 760),
        ]:
            d.text((cx - d.textlength(text, font=f) / 2, y), text, font=f, fill=color)
        return img

    # ----------------------------------------------------------------- frame

    def state(self, t: float) -> tuple[float, float, float]:
        cur = self._home()
        for t0, a, b in self.transitions:
            if t0 > t:
                break
            e = ease((t - t0) / EASE_S)
            cur = tuple(a[i] + (b[i] - a[i]) * e for i in range(3))
        # return to the full view once a focus has run out
        last = next((f for f in reversed(self.focus) if f.t <= t), None)
        if last and last.z > 1.0 and t > last.end:
            back = ease((t - last.end) / EASE_S)
            h = self._home()
            cur = tuple(cur[i] + (h[i] - cur[i]) * back for i in range(3))
        return cur

    def crop(self, t: float) -> tuple[int, int, int, int]:
        cx, cy, z = self.state(t)
        w, h = self.sw / z, self.sh / z
        x0 = min(max(0.0, cx - w / 2), self.sw - w)
        y0 = min(max(0.0, cy - h / 2), self.sh - h)
        return (int(x0), int(y0), int(x0 + w), int(y0 + h))

    def frame(self, src: Image.Image, t: float) -> Image.Image:
        gone = ease((t - self.end_start) / 0.9) if t >= self.end_start else 0.0
        out = self.bg.copy() if gone == 0 else Image.blend(self.bg, self.bg_clean, gone)
        content = src.crop(self.crop(t)).resize((WW, WH), Image.BICUBIC, reducing_gap=2.0)

        # entrance and exit of the window
        enter = ease(t / 0.9)
        leave = ease((t - self.end_start) / 0.9) if t >= self.end_start else 0.0
        vis = enter * (1 - leave)
        if vis < 0.999:
            s = 0.94 + 0.06 * vis
            w, h = int(WW * s), int(WH * s)
            small = content.resize((w, h), Image.BILINEAR)
            m = self.mask.resize((w, h)).point(lambda v: int(v * vis))
            out.paste(small, (WX + (WW - w) // 2, WY + (WH - h) // 2), m)
        else:
            out.paste(content, (WX, WY), self.mask)

        layer = out.convert("RGBA")
        if vis > 0.01:
            ImageDraw.Draw(layer).rounded_rectangle((WX, WY, WX + WW - 1, WY + WH - 1), RADIUS, outline=(255, 255, 255, int(28 * vis)), width=1)

        # chapter title, sliding in at each scene start
        i = max((k for k, s in enumerate(self.scenes) if s["start"] <= t), default=0)
        if t < self.end_start:
            sc = self.scenes[i]
            a = ease((t - sc["start"]) / 0.45)
            ch = with_alpha(self.chapters[i], a)
            layer.alpha_composite(ch, (WX + int(-24 * (1 - a)), 34))

        # rubric tracker
        for x, off, on, key in self.chips:
            if t >= self.end_start:
                break
            lit_at = self.lit.get(key)
            if lit_at is not None and t >= lit_at:
                p = min(1.0, (t - lit_at) / 0.5)
                pop = 1.0 + 0.18 * math.sin(math.pi * p) if p < 1 else 1.0
                chip = on if pop == 1.0 else on.resize((int(on.width * pop), int(on.height * pop)), Image.BICUBIC)
                layer.alpha_composite(chip, (x - (chip.width - on.width) // 2, 38 - (chip.height - on.height) // 2))
            else:
                layer.alpha_composite(off, (x, 38))

        # caption of the current focus
        f = next((f for f in reversed(self.focus) if f.t <= t < f.end), None)
        if f and f.caption_img is not None and t < self.end_start:
            a = fade(t, f.t + 0.25, f.end, 0.3)
            if a > 0:
                img = with_alpha(f.caption_img, a)
                layer.alpha_composite(img, (WX + (WW - img.width) // 2, WY + WH - img.height - 34 + int(14 * (1 - a))))

        # name lower-third at the start
        a = fade(t, 1.0, 6.8, 0.5)
        if a > 0:
            layer.alpha_composite(with_alpha(self.lower_third, a), (WX + 40 + int(-30 * (1 - a)), WY + WH - 112 - 40))

        # subtitles, in the band under the window
        sub = next(((s, e, img) for s, e, img in self.subs if s <= t < e), None)
        if sub:
            s, e, img = sub
            a = fade(t, s, e, 0.12)
            if a > 0:
                y = WY + WH + (OH - WY - WH - img.height) // 2 - 2
                layer.alpha_composite(with_alpha(img, a), ((OW - img.width) // 2, y))

        # end card
        if t >= self.end_start:
            layer.alpha_composite(with_alpha(self.end_card, ease((t - self.end_start - 0.3) / 0.9)))

        # progress bar
        d = ImageDraw.Draw(layer)
        d.rectangle((0, OH - 4, OW, OH), fill=(255, 255, 255, 14))
        d.rectangle((0, OH - 4, int(OW * min(1.0, t / max(self.total, 0.1))), OH), fill=ACCENT2 + (255,))
        return layer.convert("RGB")


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True).stderr
    import re
    size = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", out)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(size[1]), int(size[2]), int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3])


def _piecewise(points: list[tuple[float, float]]):
    """Monotone piecewise-linear map through (x, y) points; flat beyond the ends."""
    pts = sorted(points)

    def f(x: float) -> float:
        if x <= pts[0][0]:
            return pts[0][1] + (x - pts[0][0])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x <= x1:
                return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return pts[-1][1] + (x - pts[-1][0])
    return f


def compose(raw: Path, timeline: dict, out: Path, *, presenter: str, repo: str = "",
            subtitles: list[tuple[float, float, str]] | None = None,
            knots: list[tuple[float, float]] | None = None) -> Path:
    """knots: (footage time, output time) pairs, e.g. each scene's start in the footage and in the voiceover.
    Between knots the footage is sped up or slowed down a little so every scene starts on its line."""
    sw, sh, total = probe(raw)
    out_total = total
    to_src = to_out = (lambda x: x)
    if knots:
        pts = [(0.0, 0.0)] + [(float(a), float(b)) for a, b in knots if 0 < a < total] + [(total, float(knots[-1][1]) if knots[-1][0] >= total - 0.05 else total)]
        out_total = pts[-1][1]
        to_out = _piecewise(pts)
        to_src = _piecewise([(b, a) for a, b in pts])
        timeline = {**timeline,
                    "scenes": [{**sc, "start": round(to_out(sc["start"]), 3)} for sc in timeline["scenes"]],
                    "focus": [{**e, "t": round(to_out(e["t"]), 3)} for e in timeline.get("focus", [])],
                    "features": [{**e, "t": round(to_out(e["t"]), 3)} for e in timeline.get("features", [])]}
    comp = Compositor(timeline, (sw, sh), out_total, presenter, repo, subtitles)
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    dec = subprocess.Popen([ff, "-loglevel", "error", "-i", str(raw), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    enc = subprocess.Popen([ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OW}x{OH}", "-r", str(FPS),
                            "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-color_range", "tv",
                            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", str(out)],
                           stdin=subprocess.PIPE)
    size = sw * sh * 3
    have, buf = -1, None  # index and pixels of the newest decoded source frame
    try:
        for k in range(int(round(out_total * FPS))):
            t = k / FPS
            want = max(0, int(round(to_src(t) * FPS)))
            while have < want:
                nxt = dec.stdout.read(size)
                if len(nxt) < size:
                    break  # end of footage: hold the last frame
                buf, have = nxt, have + 1
            if buf is None:
                break
            frame = comp.frame(Image.frombuffer("RGB", (sw, sh), buf, "raw", "RGB", 0, 1), t)
            enc.stdin.write(frame.tobytes())
            if k and k % 300 == 0:
                print(f"    composited {t:5.0f}s / {out_total:.0f}s", flush=True)
    finally:
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
    if enc.returncode:
        raise RuntimeError("ffmpeg failed while encoding the motion pass")
    return out
