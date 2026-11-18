# Demo video

The narrated demo (about 1¾ minutes) is rendered, not screen-recorded. The script lives in
[`demo_video.yaml`](demo_video.yaml); [`scripts/make_demo_video.py`](../scripts/make_demo_video.py)
turns it into `docs/demo.mp4` plus burned-in captions and [`demo.srt`](demo.srt).

```bash
pip install pillow imageio-ffmpeg pyyaml
python scripts/make_demo_video.py                  # ElevenLabs voice (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID in .env)
python scripts/make_demo_video.py --tts say        # macOS voice instead
python scripts/make_demo_video.py --stills out/    # one PNG per scene, for checking layout
```

## Structure

Written for recruiters (the problem and result land in the first 20 seconds) and hiring
managers (design decisions and how quality is measured). Each scene in the YAML is tagged with
its part of the XYZ formula: accomplished X, measured by Y, by doing Z.

| Part | Scenes | What it shows |
| --- | --- | --- |
| Why | `hook` | What goes wrong when an agent can spend: overspending, manipulation, double charges |
| What | `result`, `happy-path`, `injection`, `merchant` | Headline metrics, then real output of `scripts/local_demo.py`: a within-policy purchase, a blocked prompt injection, declined merchant attacks |
| How | `architecture`, `evals` | The model / code / Stripe Issuing split, then the eval suite, CI gate and bugs found in review |
| Close | `close` | One-sentence XYZ summary and the repo link |

## Rules

- **No hand-typed metrics.** `{scenarios}`, `{attacks}`, `{tests}` and the rest are filled from
  `python evals/run_evals.py --json` and pytest's test count at render time. Static facts sit
  under `static:` with the file they come from.
- The renderer refuses to run while any `[[PLACEHOLDER]]` remains or a `{name}` is unknown.
- ElevenLabs clips are cached by text under `.cache/tts/`, so re-renders only pay for changed
  sentences.
- `docs/demo.mp4` is gitignored; the README plays the copy hosted on GitHub.
