# FRAUDLOOP demo video

This editable walkthrough uses real, locally captured application screens with
short editorial captions. The renderer works entirely offline with Pillow,
NumPy, and FFmpeg. It does not start the app, access models or TigerGraph, edit
answers, or change application state.

## Render

From the repository root:

```bash
# Inspect all scenes before spending time on video encoding.
.venv/bin/python demo-video/render.py --preview

# Render 1920×1080, 30 fps H.264 with original quiet transition sounds.
.venv/bin/python demo-video/render.py

# Optional faster review export, or a silent export.
.venv/bin/python demo-video/render.py --size 1280x720 --output demo-video/out/review.mp4
.venv/bin/python demo-video/render.py --no-audio --output demo-video/out/silent.mp4
```

Default inputs are `demo-video/scenes.json` and screenshot paths relative to that
file. Use `--manifest` and `--output` to select other inputs or destinations.
`--fps`, `--threads`, and `--force` control frame rate, encoder parallelism, and
cached scene reuse. A render interrupted during encoding can be resumed with
the same command; unchanged completed segments are reused.

The final folder contains:

- `fraudloop-demo.mp4`: H.264/AAC video with chapter markers and an optional
  English subtitle track. Captions are also included in the visual composition.
- `fraudloop-demo.srt`: editable caption sidecar.
- `poster.jpg` and `contact-sheet.png`: thumbnail and whole-film visual review.
- `frames/` and `thumbnails/`: full-resolution scene artwork and smaller previews.
- `transition-sounds.wav`: original synthesized transition tones, if enabled.
- `render-info.json`: output dimensions, timing, and media provenance.
- `segments/`: cached video segments. These can be removed after final delivery
  if resumable rendering is no longer needed.

No third-party music, generated voice, stock footage, or remote media is used.

## Scene manifest

The JSON may be an array or `{ "scenes": [...] }`:

```json
[
  {
    "id": "intro",
    "chapter": "FRAUDLOOP",
    "type": "hero",
    "title": "From alert to\naccountable action.",
    "caption": "Trace the evidence. Apply policy. Keep a human in control.",
    "duration": 10,
    "badge": "Agentic fraud investigation"
  },
  {
    "id": "case-evidence",
    "chapter": "Investigate",
    "type": "product",
    "title": "Every signal has a source",
    "caption": "Follow transaction evidence and inspect the recommendations before a human reviews the case.",
    "duration": 12,
    "asset": "assets/case-evidence.png",
    "layout": "auto",
    "url": "FRAUDLOOP  /  Case evidence"
  },
  {
    "id": "validation",
    "chapter": "Verification",
    "type": "proof",
    "title": "A validated foundation",
    "caption": "The saved benchmark answers stay separate from demo runs. Bank actions remain recommendations.",
    "duration": 10,
    "stats": [
      { "value": "20", "label": "valid benchmark answers" },
      { "value": "2", "label": "SAR recommendations" }
    ]
  }
]
```

Required fields: `id`, `title`, `caption`, `duration` (seconds). Scene IDs must be
unique and contain only letters, numbers, hyphens, or underscores.

Supported types are `hero`, `product`, `proof`, and `end`. Product scenes need an
`asset`. Proof scenes use up to four `stats`; providing an `asset` instead turns
the scene into a product frame. Hero/end scenes support an optional `badge` and
up to three short `tags`. Every scene accepts a `chapter`, `footer`, and `url`
(the browser-frame label, not an actual navigation).

Screenshots are contained without automatic cropping. A deliberate `focus`
crop may be set to `[x, y, width, height]`. Use all values between zero and one
for normalized image coordinates, or use source pixels. For example,
`[0.18, 0.10, 0.80, 0.46]` isolates a wide detail area. Verify every crop in the
contact sheet. `layout: "wide"` gives the image a full-width stage;
`layout: "split"` keeps an explanatory left column. The default `auto` chooses
wide only when the image/crop is at least 2.12:1.

Keep titles short, captions under roughly 180 characters, and scene durations
long enough to read them. Overflow raises an error instead of silently clipping
text. Changes to copy, screenshot crop, frame size, or timing invalidate the
corresponding cached video segment.

The visual system uses ink/navy, restrained cyan and amber, and Open Sans plus
Source Code Pro from the system. Local IBM Plex font files in `assets/` or
`assets/fonts/` are preferred when available. The entrance moves gently for
0.85 seconds. Screenshots then scale gently from 98.3% to 100% inside the
frame, preserving all visible content while captions remain still. Short navy
fades separate scenes.

## Quality check

Review the contact sheet and full-resolution focal frames, then play the final
video at output resolution. The captions should describe only the state shown
in the captured app. A saved replay, local fallback, demo role, simulated
customer reply, or recommendation must remain labeled accurately.

```bash
ffprobe -v error -show_entries format=duration,size:stream=codec_name,codec_type,width,height,r_frame_rate -of json demo-video/out/fraudloop-demo.mp4
```

Retain `render.py`, `scenes.json`, and `assets/` together to edit or reproduce the
film. Application development and media rendering are independent.
