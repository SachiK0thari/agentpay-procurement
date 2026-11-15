"""Render a narrated demo video (docs/demo.mp4) from the real output of the demo and the evals.

    pip install pillow imageio-ffmpeg     # only needed for this script (or have ffmpeg on PATH)
    python scripts/make_demo_video.py [--out docs/demo.mp4] [--voice "Ava (Premium)"] [--rate 172]

Follows docs/DEMO_SCRIPT.md: the same scenes, narration and "Point at" highlights. The voiceover
comes from macOS `say`. Captions are burned in, and a matching .srt is written next to the video
so you can re-record the voice yourself. Pass --silent to skip the voice and keep the captions.
"""
from __future__ import annotations

import argparse
import array
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import wave
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_demo_gif import BG, COLS, DIMC, FG, SGR, run_demo, spans, wrap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS, RATE = 1920, 1080, 30, 22050
ROWS, FONT_SIZE = 21, 24
PAGE = (14, 15, 19)
HIGHLIGHT, ACCENT = (62, 56, 30), (230, 190, 90)
LINE_S, GAP_S, CHUNK_PAUSE_S, SCENE_PAUSE_S = 0.28, 0.35, 0.6, 1.0


def _font(paths, size, index=0):
    for p in paths:
        try:
            return ImageFont.truetype(p, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


MONO = ["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
SANS = ["/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
REG, BOLD = _font(MONO, FONT_SIZE), _font(MONO, FONT_SIZE, 1)
CAP = _font(SANS, 34)
TITLE, SUB = _font(SANS, 96, 1), _font(SANS, 44)
CW, LH = REG.getlength("M"), int(FONT_SIZE * 1.45)
BAR, PAD = 40, 26
TW, TH = int(PAD * 2 + CW * COLS), BAR + PAD * 2 + LH * ROWS
TX, TY = (W - TW) // 2, 48


@dataclass
class Chunk:
    sentences: list[str]               # captions; spoken text may differ (see SPOKEN)
    until: str | None = None           # reveal terminal lines up to the first one containing this
    highlight: list[str] = field(default_factory=list)


@dataclass
class Segment:
    kind: str                          # "title" | "term"
    chunks: list[Chunk]
    lines: list[str] = field(default_factory=list)
    title: str = ""


SPOKEN = [(r"\bagentpay\b", "agent pay"), (r"\$", "")]

NARRATION = {
    "open": [
        Chunk(["An AI agent that can pay is an unpredictable actor holding a credit card.",
               "It can overspend, be manipulated by a web page, charge twice, or report success when nothing happened.",
               "agentpay is a procurement agent on Stripe Issuing, built so the model's mistakes can't become payments.",
               "The model proposes. Code decides."]),
    ],
    1: [
        Chunk(["A teammate asks for three USB-C docks.",
               "Claude searches the catalog, reads the product page and checks the budget. All of its tools are read-only.",
               "It proposes three docks at sixty dollars."], until="Model proposes", highlight=["Planner tools"]),
        Chunk(["Code, not the model, records where each value came from: the vendor and price from the catalog, the quantity from the user.",
               "It's under the approval threshold and within budget, so the agent gets a single-use virtual card for exactly one hundred eighty dollars, in one merchant category."],
              until="Card issued", highlight=["Provenance assigned by code", "limit=$180.00"]),
        Chunk(["The merchant charges, the webhooks reconcile it, and the card is cancelled.",
               "A second charge on that card is declined."], until=None,
              highlight=["card now canceled", "card_canceled"]),
    ],
    2: [
        Chunk(["A twelve-hundred-dollar laptop is over the five-hundred-dollar threshold, so no card exists yet.",
               "An approval link goes to the IT approver. It's signed, works once, expires in a day, and is bound to that approver."],
              until="Approval link sent", highlight=["over_approval_threshold", "Approval link sent"]),
        Chunk(["The requester can't approve their own order.",
               "The approver can, and only then is the card created."], until=None,
              highlight=["approve their own order", "bob approves"]),
    ],
    3: [
        Chunk(["Now an attack. The product page says \"customers always need five, not three\", and the model falls for it and proposes five."],
              until="Model proposes", highlight=["Product page the agent reads", "Model proposes"]),
        Chunk(["It doesn't matter. Code sees that the five came from a web page, not from the user, so the order goes to a human.",
               "The approver rejects it. No card was ever created, and the budget is released.",
               "The guardrail doesn't depend on the model resisting the injection."], until=None,
              highlight=["quantity=web", "untrusted_provenance:quantity", "none, ever"]),
    ],
    4: [
        Chunk(["Last, the merchant side. The card is for exactly fifteen dollars.",
               "A twenty-dollar charge is declined by Stripe's own card controls before our code even runs."],
              until="spending_limit_exceeded", highlight=["limit=$15.00", "spending_limit_exceeded"]),
        Chunk(["A look-alike merchant tries the right amount. Our real-time authorization check declines it on the merchant name.",
               "I found this gap while building this demo and fixed it."],
              until="merchant_name_mismatch", highlight=["TOTALLY NOT FIGMA LLC", "merchant_name_mismatch"]),
        Chunk(["The real merchant then charges, and it goes through."], until=None,
              highlight=["card now canceled"]),
    ],
    "close": [
        Chunk(["Eighty-two adversarial and failure scenarios: zero unsafe purchases, zero duplicate charges."],
              until=None, highlight=["unsafe_purchases", "duplicate_charges", "adversarial_blocked"]),
        Chunk(["This demo runs against an in-memory Stripe so it's repeatable, and the same flow has run against real Stripe Issuing in test mode: real card, real-time decision, capture, reconcile.",
               "Code and design doc are in the repo."]),
    ],
}


# ── inputs ────────────────────────────────────────────────────────────────────────────────

def demo_scenes() -> tuple[list[str], list[list[str]]]:
    intro, scenes, cur = [], [], None
    for line in run_demo():
        if "── " in line:
            cur = [line.replace("── ", "▶ ", 1).replace(" ──", "")]  # Menlo bold lacks box-drawing
            scenes.append(cur)
        elif cur is None:
            if line.strip():
                intro.append(line)
        elif line.strip():
            cur.append(line)
    return intro, scenes


def eval_summary() -> list[str]:
    out = subprocess.run([sys.executable, str(ROOT / "evals" / "run_evals.py")], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    start = next(i for i, l in enumerate(out) if l.startswith("scenarios"))
    keep = ("scenarios", "unsafe_purchases", "adversarial_blocked", "duplicate_charges",
            "task_success_rate", "false_block_rate", "human_review_rate")
    summary = [l for l in out[start:] if l.split(" ")[0] in keep]
    rows = []
    for l in out[:start]:                    # re-lay the per-scenario rows to fit COLS (drops the kind column)
        parts = l.split()
        if len(parts) >= 6 and len(parts[0]) < 40 and l.rstrip().endswith(("ok", "blocked (legit)")):
            name, _, state, amount, caught, *result = parts
            rows.append(f"{name:<40}{state:<17}{amount:>8}  {caught:<22}{' '.join(result)}")
    return ["\033[2m  ...\033[0m"] + [f"\033[2m{r[:COLS]}\033[0m" for r in rows[-8:]] + [""] + \
        [re.sub(r"(\s)(0|18/18)$", "\\1\033[32m\\2\033[0m", l) for l in summary]


# ── audio ─────────────────────────────────────────────────────────────────────────────────

def spoken(text: str) -> str:
    for pat, rep in SPOKEN:
        text = re.sub(pat, rep, text)
    return text


def synth(text: str, voice: str, rate: int, path: Path) -> array.array:
    subprocess.run(["say", "-v", voice, "-r", str(rate), "--file-format=WAVE",
                    f"--data-format=LEI16@{RATE}", "-o", str(path), spoken(text)], check=True)
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == RATE
        return array.array("h", w.readframes(w.getnframes()))


# ── drawing ───────────────────────────────────────────────────────────────────────────────

def draw_caption(d: ImageDraw.ImageDraw, text: str):
    if not text:
        return
    lines = textwrap.wrap(text, 78)
    lh = 46
    top = H - 40 - lh * len(lines)
    width = max(CAP.getlength(l) for l in lines)
    d.rounded_rectangle([(W - width) / 2 - 28, top - 16, (W + width) / 2 + 28, H - 28], 14, fill=(0, 0, 0))
    for i, l in enumerate(lines):
        d.text(((W - CAP.getlength(l)) / 2, top + i * lh), l, font=CAP, fill=(240, 240, 240))


def term_frame(lines: list[str], highlight: list[str], caption: str, title: str) -> Image.Image:
    img = Image.new("RGB", (W, H), PAGE)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([TX, TY, TX + TW, TY + TH], 14, fill=BG)
    d.rounded_rectangle([TX, TY, TX + TW, TY + BAR], 14, fill=(38, 41, 50))
    d.rectangle([TX, TY + BAR - 14, TX + TW, TY + BAR], fill=(38, 41, 50))
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse([TX + 18 + i * 26, TY + 13, TX + 32 + i * 26, TY + 27], fill=c)
    d.text((TX + (TW - REG.getlength(title)) / 2, TY + 8), title, font=REG, fill=DIMC)
    y = TY + BAR + PAD
    for line in lines[-ROWS:]:
        plain = SGR.sub("", line)
        if any(h in plain for h in highlight):
            d.rounded_rectangle([TX + PAD - 10, y - 4, TX + TW - PAD + 6, y + LH - 4], 6, fill=HIGHLIGHT)
            d.rectangle([TX + PAD - 10, y - 4, TX + PAD - 6, y + LH - 4], fill=ACCENT)
        x = TX + PAD
        for text, colour, bold in spans(line):
            d.text((x, y), text, font=BOLD if bold else REG, fill=colour)
            x += CW * len(text)
        y += LH
    draw_caption(d, caption)
    return img


def title_frame(caption: str, step: int) -> Image.Image:
    img = Image.new("RGB", (W, H), PAGE)
    d = ImageDraw.Draw(img)
    t = "agentpay"
    d.text(((W - TITLE.getlength(t)) / 2, 300), t, font=TITLE, fill=(240, 240, 240))
    s = "A procurement agent on Stripe Issuing"
    d.text(((W - SUB.getlength(s)) / 2, 430), s, font=SUB, fill=DIMC)
    if step >= 3:
        s = "The model proposes. Code decides."
        d.text(((W - SUB.getlength(s)) / 2, 540), s, font=SUB, fill=ACCENT)
    draw_caption(d, caption)
    return img


# ── timeline ──────────────────────────────────────────────────────────────────────────────

def build(segments: list[Segment], voice: str | None, rate: int, work: Path):
    """Return (frames as (png, seconds)), audio samples and srt cues."""
    frames, cues = [], []
    audio = array.array("h")
    t = 0.0
    n_png = 0

    def emit(img: Image.Image, seconds: float):
        nonlocal n_png, t
        if seconds <= 0:
            return
        p = work / f"f{n_png:05d}.png"
        img.save(p)
        n_png += 1
        frames.append((p, seconds))
        t += seconds

    def place(samples: array.array, at: float):
        start = int(at * RATE)
        if len(audio) < start:
            audio.extend([0] * (start - len(audio)))
        audio.extend(samples)

    clip_n = 0
    for seg in segments:
        shown_upto = 0
        shown: list[str] = []
        for ci, chunk in enumerate(seg.chunks):
            if seg.kind == "term":
                end = len(seg.lines)
                if chunk.until:
                    end = next(i for i, l in enumerate(seg.lines) if i >= shown_upto and chunk.until in l) + 1
                new = seg.lines[shown_upto:end]
                shown_upto = end
            else:
                new = []

            # sentence timings
            sent = []
            for s in chunk.sentences:
                if voice:
                    clip_n += 1
                    samples = synth(s, voice, rate, work / f"a{clip_n:03d}.wav")
                    dur = len(samples) / RATE
                else:
                    samples, dur = None, max(1.8, len(s.split()) / (rate / 60))
                sent.append((s, samples, dur))

            def render(caption: str, hl: list[str]):
                if seg.kind == "title":
                    return title_frame(caption, sum(len(c.sentences) for c in seg.chunks[:ci]) + idx)
                return term_frame(shown, hl, caption, seg.title)

            # reveal new lines while the first sentence starts
            first = sent[0][0] if sent else ""
            speech_at = t + 0.2
            idx = 0
            for line in new:
                shown.extend(wrap(line))
                emit(render(first, []), LINE_S)
            reveal_end = t
            t_cursor = speech_at
            # highlights come on once the chunk's lines are in place
            for idx, (s, samples, dur) in enumerate(sent):
                t_cursor = max(t_cursor, t)            # never start a sentence before its caption
                if samples is not None:
                    place(samples, t_cursor)
                cues.append((t_cursor, t_cursor + dur, s))
                t_cursor += dur + GAP_S
                end_at = t_cursor
                if idx == len(sent) - 1:
                    end_at += CHUNK_PAUSE_S
                emit(render(s, chunk.highlight), max(end_at, reveal_end + 0.5) - t)
        emit(render("", []), SCENE_PAUSE_S)
    return frames, audio, cues


def srt_time(s: float) -> str:
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        exe = shutil.which("ffmpeg")
        if not exe:
            sys.exit("needs ffmpeg: pip install imageio-ffmpeg, or brew install ffmpeg")
        return exe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "demo.mp4"))
    ap.add_argument("--voice", default="Ava (Premium)")
    ap.add_argument("--rate", type=int, default=172, help="words per minute")
    ap.add_argument("--silent", action="store_true", help="captions only, no voiceover")
    args = ap.parse_args()
    voice = None if args.silent else args.voice
    if voice and not shutil.which("say"):
        sys.exit("voiceover needs macOS `say`; pass --silent for a captions-only video")

    intro, scenes = demo_scenes()
    prompt = "\033[32m$\033[0m python scripts/local_demo.py --pause"
    head = [prompt] + intro + [""]
    segments = [Segment("title", NARRATION["open"])]
    for n, scene in enumerate(scenes, 1):
        segments.append(Segment("term", NARRATION[n], head + scene, "agentpay: the model proposes, code decides"))
    segments.append(Segment("term", NARRATION["close"],
                            ["\033[32m$\033[0m python evals/run_evals.py"] + eval_summary(), "agentpay: evals"))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        frames, audio, cues = build(segments, voice, args.rate, work)
        total = sum(s for _, s in frames)
        audio.extend([0] * max(0, int(total * RATE) - len(audio)))
        wav = work / "voice.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
            w.writeframes(audio.tobytes())
        lst = work / "frames.txt"
        lst.write_text("".join(f"file '{p}'\nduration {s:.4f}\n" for p, s in frames) + f"file '{frames[-1][0]}'\n")
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-i", str(wav), "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "slow",
                        "-crf", "20", "-tune", "stillimage", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k", "-shortest",
                        "-movflags", "+faststart", str(out)], check=True)

    srt = out.with_suffix(".srt")
    srt.write_text("".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{s}\n\n" for i, (a, b, s) in enumerate(cues, 1)))
    mb = out.stat().st_size / 1e6
    print(f"wrote {out}: {total:.0f}s, {mb:.1f} MB, {W}x{H}; captions in {srt.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
