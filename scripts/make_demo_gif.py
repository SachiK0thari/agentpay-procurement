"""Render docs/demo.gif from the real output of scripts/local_demo.py.

    pip install pillow            # only needed for this script
    python scripts/make_demo_gif.py [--out docs/demo.gif]

No screen recorder needed: it runs the demo, keeps its ANSI colours, and draws each scene as a
terminal window that fills in line by line. Re-run it whenever the demo output changes.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
COLS, ROWS = 104, 22                 # terminal size in characters
FONT_SIZE, PAD, BAR = 15, 18, 30    # px
BG, FG, DIMC = (24, 26, 32), (220, 223, 228), (128, 134, 146)
COLOURS = {"32": (110, 200, 120), "31": (240, 100, 95), "33": (230, 190, 90)}
LINE_MS, SCENE_HOLD_MS, END_HOLD_MS = 420, 2600, 4500
SGR = re.compile(r"\033\[(\d+)m")


def font(bold: bool):
    for path in ("/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(path, FONT_SIZE, index=1 if bold and path.endswith(".ttc") else 0)
        except OSError:
            continue
    return ImageFont.load_default()


REG, BOLD = font(False), font(True)
CW = int(REG.getlength("M"))
LH = int(FONT_SIZE * 1.45)
W, H = PAD * 2 + CW * COLS, BAR + PAD * 2 + LH * ROWS


def spans(line: str):
    """Split an ANSI-coloured line into (text, colour, bold) runs."""
    colour, bold, pos, out = FG, False, 0, []
    for m in SGR.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], colour, bold))
        code = m.group(1)
        if code == "0":
            colour, bold = FG, False
        elif code == "1":
            bold = True
        elif code == "2":
            colour = DIMC
        elif code in COLOURS:
            colour = COLOURS[code]
        pos = m.end()
    out.append((line[pos:], colour, bold))
    return out


def wrap(line: str) -> list[str]:
    """Wrap on visible width, carrying the active colour codes onto continuation lines."""
    visible = SGR.sub("", line)
    if len(visible) <= COLS:
        return [line]
    out, active, cur, width = [], "", "", 0
    for tok in re.split(r"(\033\[\d+m)", line):
        if SGR.fullmatch(tok or "x"):
            active = "" if tok == "\033[0m" else active + tok
            cur += tok
            continue
        for ch in tok:
            if width == COLS:
                cut = cur.rfind(" ")            # break at the last space (escapes contain none)
                head, tail = (cur[:cut], cur[cut + 1:]) if cut > len(active) + 2 else (cur, "")
                out.append(head + "\033[0m")
                cur = active + "  " + tail
                width = 2 + len(SGR.sub("", tail))
            cur += ch
            width += 1
    out.append(cur)
    return out


def frame(lines: list[str], title: str) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, BAR], fill=(38, 41, 50))
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse([14 + i * 20, 10, 26 + i * 20, 22], fill=c)
    tw = REG.getlength(title)
    d.text(((W - tw) / 2, 7), title, font=REG, fill=DIMC)
    y = BAR + PAD
    for line in lines[-ROWS:]:
        x = PAD
        for text, colour, bold in spans(line):
            d.text((x, y), text, font=BOLD if bold else REG, fill=colour)
            x += CW * len(text)
        y += LH
    return img


def run_demo() -> list[str]:
    env = dict(os.environ, FORCE_COLOR="1")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "local_demo.py")], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "demo.gif"))
    args = ap.parse_args()

    raw = run_demo()
    intro, scenes, cur = [], [], None
    for line in raw:
        if "── " in line:
            cur = [line.replace("── ", "▶ ", 1).replace(" ──", "")]  # Menlo bold lacks box-drawing
            scenes.append(cur)
        elif cur is None:
            if line.strip():
                intro.append(line)
        elif line.strip():
            cur.append(line)

    prompt = "\033[32m$\033[0m python scripts/local_demo.py"
    frames, durations = [], []

    def add(lines, title, ms):
        frames.append(frame(lines, title))
        durations.append(ms)

    title = "agentpay: the model proposes, code decides"
    for n in range(0, len(prompt) + 1, 3):            # type the command
        add([prompt[:n] if SGR.sub("", prompt[:n]) == prompt[:n] else prompt[:n]], title, 40)
    head = [prompt] + intro
    add(head, title, 900)

    for scene in scenes:
        shown = list(head) + [""]
        for line in scene:
            shown.extend(wrap(line))
            add(shown, title, LINE_MS)
        durations[-1] = SCENE_HOLD_MS
    durations[-1] = END_HOLD_MS

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(args.out, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   optimize=True, disposal=1)
    kb = Path(args.out).stat().st_size // 1024
    print(f"wrote {args.out}: {len(frames)} frames, {sum(durations) / 1000:.0f}s, {kb} KB, {W}x{H}px")
    return 0


if __name__ == "__main__":
    sys.exit(main())
