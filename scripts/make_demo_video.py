"""Render the narrated demo video (docs/agentpay-procurment-demo.mp4) from docs/demo_video.yaml.

    pip install pillow imageio-ffmpeg pyyaml      # only needed for this script
    python scripts/make_demo_video.py [--out docs/agentpay-procurment-demo.mp4] [--voice "Ava (Premium)"] [--rate 178]

The script file holds the scenes (tagged why / what / how / close), the narration, on-screen
callouts and the outro card. Terminal scenes are the real output of scripts/local_demo.py and
evals/run_evals.py; every metric is filled in from a live eval run and pytest's test count, so no
number in the video is typed by hand. The voiceover comes from macOS `say`. Captions are burned in,
and a matching .srt is written next to the video. Pass --silent to skip the voice.

The voice is ElevenLabs when ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are set (in .env or the
shell); clips are cached by text under .cache/tts so re-renders don't re-bill. --tts say uses macOS
`say` instead.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import os
import json
import re
import shutil
import string
import subprocess
import sys
import tempfile
import textwrap
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_demo_gif import BG, COLS, DIMC, SGR, run_demo, spans, wrap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS, RATE = 1920, 1080, 30, 22050
ROWS, FONT_SIZE = 17, 24
PAGE, WHITE = (14, 15, 19), (240, 240, 240)
HIGHLIGHT, ACCENT, RED = (62, 56, 30), (230, 190, 90), (240, 110, 100)
LINE_S, LEAD_S, GAP_S, CHUNK_PAUSE_S, SCENE_PAUSE_S = 0.22, 0.25, 0.3, 0.45, 0.55


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
TITLE, HEAD, SUB = _font(SANS, 96, 1), _font(SANS, 68, 1), _font(SANS, 44)
STAT, LABEL, NOTE, SMALL = _font(SANS, 104, 1), _font(SANS, 32), _font(SANS, 34), _font(SANS, 24)
BOX_T, BOX_S, CALL_T, CALL_S = _font(SANS, 28, 1), _font(SANS, 22), _font(SANS, 40, 1), _font(SANS, 29)
CW, LH = REG.getlength("M"), int(FONT_SIZE * 1.45)
BAR, PAD = 40, 26
TW, TH = int(PAD * 2 + CW * COLS), BAR + PAD * 2 + LH * ROWS
TX, TY = (W - TW) // 2, 40

SPOKEN = [(r"\bagentpay\b", "agent pay"), (r"\bCI\b", "C.I."), (r"\$", "")]


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


def metrics() -> dict[str, object]:
    """The numbers the script may quote, all from live runs."""
    out = subprocess.run([sys.executable, str(ROOT / "evals" / "run_evals.py"), "--json"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["summary"]
    tests = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout
    n_tests = int(re.search(r"(\d+) tests? collected", tests).group(1))
    blocked, attacks = s["adversarial_blocked"].split("/")
    return {"scenarios": s["scenarios"], "unsafe": s["unsafe_purchases"], "duplicates": s["duplicate_charges"],
            "attacks_blocked": s["adversarial_blocked"], "attacks": int(attacks), "blocked": int(blocked),
            "tests": n_tests}


def eval_lines() -> list[str]:
    out = subprocess.run([sys.executable, str(ROOT / "evals" / "run_evals.py")], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    start = next(i for i, l in enumerate(out) if l.startswith("scenarios"))
    keep = ("scenarios", "unsafe_purchases", "adversarial_blocked", "duplicate_charges",
            "task_success_rate", "human_review_rate")
    summary = [l for l in out[start:] if l.split(" ")[0] in keep]
    rows = []
    for l in out[:start]:                    # re-lay the per-scenario rows to fit COLS (drops the kind column)
        parts = l.split()
        if len(parts) >= 6 and len(parts[0]) < 40 and l.rstrip().endswith(("ok", "blocked (legit)")):
            name, _, state, amount, caught, *result = parts
            rows.append(f"{name:<40}{state:<17}{amount:>8}  {caught:<22}{' '.join(result)}")
    return (["\033[32m$\033[0m python evals/run_evals.py", "\033[2m  ...\033[0m"]
            + [f"\033[2m{r[:COLS]}\033[0m" for r in rows[-7:]] + [""]
            + [re.sub(r"(\s)(0|\d+/\d+)$", "\\1\033[32m\\2\033[0m", l) for l in summary])


class Strict(dict):
    def __missing__(self, key):
        raise SystemExit(f"script uses {{{key}}}, which is not a known metric or static value")


def fill(obj, values: Strict):
    if isinstance(obj, str):
        if "[[" in obj:
            raise SystemExit(f"placeholder left in the script: {obj!r}")
        return string.Formatter().vformat(obj, (), values)
    if isinstance(obj, list):
        return [fill(o, values) for o in obj]
    if isinstance(obj, dict):
        return {k: fill(v, values) for k, v in obj.items()}
    return obj


# ── audio ─────────────────────────────────────────────────────────────────────────────────

def spoken(text: str) -> str:
    for pat, rep in SPOKEN:
        text = re.sub(pat, rep, text)
    return text


ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_{rate}"
ELEVEN_MODEL = "eleven_multilingual_v2"
TTS_CACHE = ROOT / ".cache" / "tts"


def synth_eleven(text: str, voice_id: str) -> array.array:
    """16-bit mono PCM at RATE from ElevenLabs, cached by (voice, model, text)."""
    key = hashlib.sha256(f"{voice_id}|{ELEVEN_MODEL}|{RATE}|{text}".encode()).hexdigest()[:24]
    cached = TTS_CACHE / f"{key}.pcm"
    if not cached.exists():
        r = httpx.post(ELEVEN_URL.format(voice=voice_id, rate=RATE), timeout=120,
                       headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                       json={"text": text, "model_id": ELEVEN_MODEL,
                             "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}})
        if r.status_code != 200:
            raise SystemExit(f"ElevenLabs error {r.status_code}: {r.text[:300]}")
        TTS_CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(r.content)
    samples = array.array("h")
    samples.frombytes(cached.read_bytes())
    return samples


def synth(text: str, voice: str, rate: int, path: Path) -> array.array:
    if voice.startswith("eleven:"):
        return synth_eleven(spoken(text), voice.removeprefix("eleven:"))
    subprocess.run(["say", "-v", voice, "-r", str(rate), "--file-format=WAVE",
                    f"--data-format=LEI16@{RATE}", "-o", str(path), spoken(text)], check=True)
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == RATE
        return array.array("h", w.readframes(w.getnframes()))


# ── drawing ───────────────────────────────────────────────────────────────────────────────

def _centred(d: ImageDraw.ImageDraw, text: str, y: float, font, fill, cx: float = W / 2):
    d.text((cx - font.getlength(text) / 2, y), text, font=font, fill=fill)


def draw_caption(d: ImageDraw.ImageDraw, text: str):
    if not text:
        return
    lines = textwrap.wrap(text, 80)
    lh = 46
    top = H - 36 - lh * len(lines)
    width = max(CAP.getlength(l) for l in lines)
    d.rounded_rectangle([(W - width) / 2 - 28, top - 14, (W + width) / 2 + 28, H - 24], 14, fill=(0, 0, 0))
    for i, l in enumerate(lines):
        d.text(((W - CAP.getlength(l)) / 2, top + i * lh), l, font=CAP, fill=WHITE)


def draw_callout(d: ImageDraw.ImageDraw, callout: list[str] | None, y: float):
    """Lower-third metric card, left-aligned under the terminal."""
    if not callout:
        return
    main, *rest = callout
    w = max([CALL_T.getlength(main)] + [CALL_S.getlength(r) for r in rest]) + 64
    h = 30 + 50 + 40 * len(rest)
    d.rounded_rectangle([TX, y, TX + w, y + h], 12, fill=(30, 30, 24))
    d.rectangle([TX, y, TX + 8, y + h], fill=ACCENT)
    d.text((TX + 34, y + 16), main, font=CALL_T, fill=ACCENT)
    for i, r in enumerate(rest):
        d.text((TX + 34, y + 68 + i * 40), r, font=CALL_S, fill=(200, 200, 200))


def terminal(d: ImageDraw.ImageDraw, lines: list[str], highlight: list[str], title: str):
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


def problem_card(d: ImageDraw.ImageDraw, step: int):
    _centred(d, "What happens when an AI agent can spend money?", 230, HEAD, WHITE)
    risks = [("Overspends", "30 laptops instead of 3"),
             ("Gets manipulated", "a web page says \"order 5, not 3\""),
             ("Charges twice", "a timeout triggers a retry")]
    col = 560
    for i, (head, sub) in enumerate(risks):
        cx = W / 2 + (i - 1) * col
        d.rounded_rectangle([cx - 250, 390, cx + 250, 590], 18, fill=(32, 22, 22), outline=(90, 45, 42), width=2)
        _centred(d, head, 430, CALL_T, RED, cx)
        _centred(d, sub, 500, CALL_S, (200, 200, 200), cx)
    if step >= 1:
        _centred(d, "Better prompts don't fix that.", 680, SUB, ACCENT)


def stats_card(d: ImageDraw.ImageDraw, step: int, m: dict):
    _centred(d, "agentpay", 120, TITLE, WHITE)
    _centred(d, "A purchasing agent on Stripe Issuing", 250, SUB, DIMC)
    _centred(d, "The model proposes. Code decides.", 330, SUB, ACCENT)
    if step >= 1:
        tiles = [(str(m["unsafe"]), "unsafe purchases"), (str(m["duplicates"]), "duplicate charges"),
                 (m["attacks_blocked"], "attacks blocked"), (str(m["scenarios"]), "test scenarios")]
        col = 400
        for i, (num, label) in enumerate(tiles):
            cx = W / 2 + (i - 1.5) * col
            d.rounded_rectangle([cx - 180, 450, cx + 180, 690], 18, fill=(26, 27, 33))
            _centred(d, num, 480, STAT, ACCENT if i < 3 else WHITE, cx)
            _centred(d, label, 615, LABEL, DIMC, cx)
        _centred(d, "source: python evals/run_evals.py", 730, SMALL, DIMC)


# diagram: three lanes, snake layout. (lane, column, title, subtitle)
LANES = [("model", "Model", "untrusted"), ("code", "My code", "deterministic, tested"),
         ("stripe", "Stripe Issuing", "the card itself")]
BOXES = [
    ("model", 0, "User request", "\"3 USB-C docks\""),
    ("model", 1, "Claude planner", "read-only tools"),
    ("model", 2, "One proposal", "no card, no decision"),
    ("code", 0, "Provenance", "who said each value"),
    ("code", 1, "Policy", "caps, vendor allowlist"),
    ("code", 2, "Risk", "can only escalate"),
    ("code", 3, "Budget", "atomic reservation"),
    ("code", 4, "Approval", "signed, single-use"),
    ("stripe", 4, "Single-use card", "exact amount, one MCC"),
    ("stripe", 3, "Real-time auth", "amount, MCC, merchant"),
    ("stripe", 2, "Webhooks + ledger", "signed, deduped"),
]
DX0, BW, BH, BGAP = 340, 276, 118, 28
LANE_Y = {"model": 200, "code": 455, "stripe": 710}


def _box_xy(lane: str, col: int) -> tuple[float, float]:
    return DX0 + col * (BW + BGAP), LANE_Y[lane]


def _arrow(d: ImageDraw.ImageDraw, pts: list[tuple[float, float]], colour):
    d.line(pts, fill=colour, width=4, joint="curve")
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    if x1 == x2:
        s = 1 if y2 > y1 else -1
        d.polygon([(x2, y2), (x2 - 10, y2 - 16 * s), (x2 + 10, y2 - 16 * s)], fill=colour)
    else:
        s = 1 if x2 > x1 else -1
        d.polygon([(x2, y2), (x2 - 16 * s, y2 - 10), (x2 - 16 * s, y2 + 10)], fill=colour)


def diagram_card(d: ImageDraw.ImageDraw, active: list[str]):
    _centred(d, "How it works", 60, HEAD, WHITE)
    lit = lambda lane: not active or lane in active  # noqa: E731
    for lane, name, note in LANES:
        y = LANE_Y[lane]
        on = lit(lane)
        d.rounded_rectangle([70, y - 26, W - 70, y + BH + 26], 20, fill=(24, 25, 30) if on else (18, 19, 23))
        d.text((100, y + 26), name, font=BOX_T, fill=ACCENT if on and active else WHITE if on else (90, 92, 100))
        d.text((100, y + 66), note, font=BOX_S, fill=DIMC if on else (70, 72, 80))
    for lane, col, title, sub in BOXES:
        x, y = _box_xy(lane, col)
        on, hot = lit(lane), bool(active) and lane in active
        d.rounded_rectangle([x, y, x + BW, y + BH], 14, fill=(40, 38, 28) if hot else (34, 36, 44) if on else (26, 27, 32),
                            outline=ACCENT if hot else None, width=3)
        _centred(d, title, y + 26, BOX_T, WHITE if on else (95, 97, 105), x + BW / 2)
        _centred(d, sub, y + 70, BOX_S, (200, 200, 200) if on else (75, 77, 85), x + BW / 2)
    arrow = (150, 150, 160)
    for lane, cols in (("model", range(2)), ("code", range(4))):
        for c in cols:
            x, y = _box_xy(lane, c)
            _arrow(d, [(x + BW + 2, y + BH / 2), (x + BW + BGAP - 2, y + BH / 2)], arrow)
    for c in (4, 3):                                       # stripe lane runs right to left
        x, y = _box_xy("stripe", c)
        _arrow(d, [(x - 2, y + BH / 2), (x - BGAP + 2, y + BH / 2)], arrow)
    px, py = _box_xy("model", 2)                           # proposal -> provenance (elbow)
    qx, qy = _box_xy("code", 0)
    mid = (py + BH + qy) / 2
    _arrow(d, [(px + BW / 2, py + BH), (px + BW / 2, mid), (qx + BW / 2, mid), (qx + BW / 2, qy)], arrow)
    ax, ay = _box_xy("code", 4)                            # approval -> card
    _arrow(d, [(ax + BW / 2, ay + BH), (ax + BW / 2, LANE_Y["stripe"])], arrow)


def outro_card(d: ImageDraw.ImageDraw, o: dict, m: dict):
    _centred(d, o["title"], 150, TITLE, WHITE)
    _centred(d, o["subtitle"], 280, SUB, DIMC)
    rows = [("Built", "a purchasing agent that can't overspend"),
            ("Measured", f"{m['unsafe']} unsafe purchases · {m['duplicates']} duplicate charges · "
                         f"{m['scenarios']} scenarios · {m['tests']} tests"),
            ("By", "the model only proposes; tested code and single-use cards decide")]
    for i, (k, v) in enumerate(rows):
        y = 400 + i * 80
        d.text((W / 2 - 640, y), k, font=CALL_T, fill=ACCENT)
        d.text((W / 2 - 400, y + 6), v, font=NOTE, fill=WHITE)
    personal = [o[k] for k in ("name", "role", "linkedin", "demo") if o.get(k)]   # optional, config-driven
    for i, value in enumerate(personal):
        _centred(d, value, 680 + i * 50, NOTE, DIMC)
    _centred(d, o["repo"], 900 if personal else 760, SUB, ACCENT)


# ── timeline ──────────────────────────────────────────────────────────────────────────────

@dataclass
class State:
    lines: list[str]
    highlight: bool
    caption: str
    sentence: int          # index of the sentence being spoken within the scene


def render(scene: dict, chunk: dict, st: State, ctx: dict) -> Image.Image:
    img = Image.new("RGB", (W, H), PAGE)
    d = ImageDraw.Draw(img)
    kind = scene["kind"]
    if kind in ("terminal", "evals"):
        title = "agentpay: evals" if kind == "evals" else "agentpay: the model proposes, code decides"
        terminal(d, st.lines, chunk.get("highlight", []) if st.highlight else [], title)
        draw_callout(d, chunk.get("callout") if st.highlight else None, TY + TH + 22)
    elif kind == "problem":
        problem_card(d, st.sentence)
    elif kind == "stats":
        stats_card(d, st.sentence, ctx["metrics"])
    elif kind == "diagram":
        diagram_card(d, chunk.get("highlight", []) if st.highlight else [])
    elif kind == "outro":
        outro_card(d, ctx["outro"], ctx["metrics"])
    else:
        raise SystemExit(f"unknown scene kind {kind!r}")
    draw_caption(d, st.caption)
    return img


def scene_lines(scene: dict, ctx: dict) -> list[str]:
    if scene["kind"] == "evals":
        return ctx["eval_lines"]
    if scene["kind"] != "terminal":
        return []
    lines = ctx["scenes"][scene["demo_scene"] - 1]
    if scene.get("skip_until"):
        cut = next(i for i, l in enumerate(lines) if scene["skip_until"] in l)
        lines = [lines[0], "\033[2m  ...\033[0m"] + lines[cut:]
    return lines


def build(scenes: list[dict], ctx: dict, voice: str | None, rate: int, work: Path):
    """Return frames as (png, seconds), audio samples, srt cues and per-scene timings."""
    frames: list[tuple[Path, float]] = []
    cues, timings = [], []
    audio = array.array("h")
    t = 0.0
    n = 0
    last_key = None

    def emit(scene, chunk, st: State, seconds: float):
        nonlocal n, t, last_key
        if seconds <= 0.001:
            return
        key = (id(scene), id(chunk), len(st.lines), st.highlight, st.caption, st.sentence)
        if key == last_key:                       # same picture: just extend the previous frame
            p, s = frames[-1]
            frames[-1] = (p, s + seconds)
        else:
            p = work / f"f{n:05d}.png"
            render(scene, chunk, st, ctx).save(p)
            n += 1
            frames.append((p, seconds))
            last_key = key
        t += seconds

    def place(samples: array.array, at: float):
        start = int(at * RATE)
        if len(audio) < start:
            audio.extend([0] * (start - len(audio)))
        audio.extend(samples)

    clip = 0
    for scene in scenes:
        start = t
        all_lines = scene_lines(scene, ctx)
        head = [] if scene["kind"] != "terminal" else ctx["head"]
        shown_upto, shown = 0, list(head)
        sent_i = 0
        for chunk in scene["chunks"]:
            end = len(all_lines)
            if chunk.get("until"):
                end = next(i for i, l in enumerate(all_lines) if i >= shown_upto and chunk["until"] in l) + 1
            new = all_lines[shown_upto:end] if all_lines else []
            shown_upto = end

            sents = []
            for s in chunk["say"]:
                if voice:
                    clip += 1
                    samples = synth(s, voice, rate, work / f"a{clip:03d}.wav")
                    sents.append((s, samples, len(samples) / RATE))
                else:
                    sents.append((s, None, max(1.8, len(s.split()) / (rate / 60))))

            c0 = t
            reveal_end = c0 + LINE_S * len(new)
            # speech starts shortly after the chunk does, while its lines reveal
            starts, cur = [], c0 + LEAD_S
            for s, samples, dur in sents:
                starts.append(cur)
                if samples is not None:
                    place(samples, cur)
                cues.append((cur, cur + dur, s))
                cur += dur + GAP_S
            chunk_end = max(cur - GAP_S + CHUNK_PAUSE_S, reveal_end + 0.6)

            # events: each new line, highlight on, each sentence start
            # line j appears at c0 + j * LINE_S; highlights come on once all of them are in
            events = sorted({c0 + LINE_S * j for j in range(1, len(new) + 1)} | set(starts) | {chunk_end})
            base = list(shown)
            prev = c0
            for ev in events:
                if ev <= prev:
                    continue
                k = min(len(new), int((prev - c0) / LINE_S + 1e-6) + 1) if new else 0
                lines = base + [w for ln in new[:k] for w in wrap(ln)]
                si = max([i for i, s0 in enumerate(starts) if s0 <= prev] or [0])
                emit(scene, chunk, State(lines, prev >= reveal_end, sents[si][0], sent_i + si), ev - prev)
                prev = ev
            shown = base + [w for ln in new for w in wrap(ln)]
            sent_i += len(sents)
        emit(scene, chunk, State(shown, True, "", sent_i - 1), SCENE_PAUSE_S)
        timings.append((scene["id"], scene["xyz"], start, t))
    return frames, audio, cues, timings


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
    ap.add_argument("--script", default=str(ROOT / "docs" / "demo_video.yaml"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "agentpay-procurment-demo.mp4"))
    ap.add_argument("--tts", choices=["eleven", "say"], default="eleven",
                    help="voice engine (default: ElevenLabs, needs ELEVENLABS_API_KEY/ELEVENLABS_VOICE_ID)")
    ap.add_argument("--voice", help="ElevenLabs voice id, or macOS voice with --tts say")
    ap.add_argument("--rate", type=int, help="words per minute (default: the script's)")
    ap.add_argument("--silent", action="store_true", help="captions only, no voiceover")
    ap.add_argument("--stills", help="write one PNG per scene to this directory and stop (for review)")
    args = ap.parse_args()

    spec = yaml.safe_load(Path(args.script).read_text())
    m = metrics()
    values = Strict({**m, **spec.get("static", {})})
    scenes = fill(spec["scenes"], values)
    outro = fill(spec["outro"], values)
    rate = args.rate or spec.get("rate", 178)
    voice: str | None = None
    if args.silent:
        pass
    elif args.tts == "eleven":
        from agentpay.config import load_dotenv
        load_dotenv(ROOT / ".env")
        vid = args.voice or os.environ.get("ELEVENLABS_VOICE_ID")
        if not (vid and os.environ.get("ELEVENLABS_API_KEY")):
            sys.exit("ElevenLabs needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID (or --tts say)")
        voice = f"eleven:{vid}"
    else:
        voice = args.voice or spec.get("say_voice", "Ava (Premium)")
        if not shutil.which("say"):
            sys.exit("--tts say needs macOS `say`; pass --silent for a captions-only video")

    intro, demo = demo_scenes()
    ctx = {"metrics": m, "outro": outro, "scenes": demo, "eval_lines": eval_lines(),
           "head": ["\033[32m$\033[0m python scripts/local_demo.py --pause"] + intro + [""]}

    if args.stills:
        out = Path(args.stills)
        out.mkdir(parents=True, exist_ok=True)
        for sc in scenes:
            lines = ctx["head"] + scene_lines(sc, ctx) if sc["kind"] == "terminal" else scene_lines(sc, ctx)
            ch = sc["chunks"][-1]
            st = State([w for ln in lines for w in wrap(ln)], True, ch["say"][-1], 99)
            render(sc, ch, st, ctx).save(out / f"{sc['id']}.png")
        print(f"wrote {len(scenes)} stills to {out}")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        frames, audio, cues, timings = build(scenes, ctx, voice, rate, work)
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
                        "-crf", "20", "-tune", "stillimage", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k",
                        "-shortest", "-movflags", "+faststart", str(out)], check=True)

    srt = out.with_suffix(".srt")
    srt.write_text("".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{s}\n\n" for i, (a, b, s) in enumerate(cues, 1)))
    for sid, xyz, a, b in timings:
        print(f"  {a:6.1f}-{b:6.1f}s  {xyz:<5} {sid}")
    mb = out.stat().st_size / 1e6
    print(f"wrote {out}: {total:.0f}s, {mb:.1f} MB, {W}x{H}; captions in {srt.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
