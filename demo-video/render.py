#!/usr/bin/env python3
"""Render the editable FRAUDLOOP walkthrough from local screenshots.

Only the selected output directory is written. No application, graph, model,
network, browser, or dataset operation is performed by this renderer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parent
BASE_SIZE = (1920, 1080)
INK = (7, 14, 27)
PANEL = (13, 25, 42)
LINE = (41, 60, 79)
WHITE = (233, 241, 248)
MUTED = (159, 179, 198)
CYAN = (100, 224, 221)
AMBER = (235, 180, 86)
FONT_DIR = Path("/usr/share/fonts/open-sans")


def font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """Prefer installed IBM Plex fonts, with offline system-font fallbacks."""
    names = {
        "regular": ("IBMPlexSans-Regular.ttf", "OpenSans-Regular.ttf"),
        "medium": ("IBMPlexSans-Medium.ttf", "OpenSans-Semibold.ttf"),
        "bold": ("IBMPlexSans-Bold.ttf", "OpenSans-Bold.ttf"),
        "mono": ("IBMPlexMono-Regular.ttf", "SourceCodePro-Regular.otf"),
    }
    plex, fallback = names[weight]
    candidates = [ROOT / "assets" / plex, ROOT / "assets" / "fonts" / plex]
    if weight == "mono":
        candidates += [Path("/usr/share/fonts/adobe-source-code-pro-fonts") / fallback]
    else:
        candidates += [FONT_DIR / fallback]
    candidates += [Path("/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf")]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.truetype("DejaVuSans.ttf", size)


def wrap(text: str, selected_font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if selected_font.getlength(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            if selected_font.getlength(word) > width:
                raise ValueError(f"Text contains a word too wide for its frame: {word!r}")
            current = word
        if current:
            lines.append(current)
    return lines


def text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    width: int,
    *,
    size: int = 30,
    minimum: int | None = None,
    weight: str = "regular",
    fill: tuple[int, int, int] = WHITE,
    max_lines: int | None = None,
    leading: float = 1.4,
) -> int:
    while True:
        selected_font = font(size, weight)
        lines = wrap(text, selected_font, width)
        if max_lines is None or len(lines) <= max_lines:
            break
        if minimum is not None and size > minimum:
            size -= 1
        else:
            raise ValueError(f"Text needs {len(lines)} lines; frame allows {max_lines}: {text!r}")
    line_height = round(size * leading)
    for index, line in enumerate(lines):
        draw.text((xy[0], xy[1] + index * line_height), line, font=selected_font, fill=fill)
    return line_height * len(lines)


def background() -> Image.Image:
    width, height = BASE_SIZE
    yy, xx = np.mgrid[0:height, 0:width]
    glow = np.exp(-(((xx - 1570) / 860) ** 2 + ((yy - 390) / 700) ** 2))
    secondary = np.exp(-(((xx - 150) / 950) ** 2 + ((yy - 1030) / 440) ** 2))
    canvas = np.empty((height, width, 3), dtype=np.float32)
    for channel, base in enumerate(INK):
        canvas[:, :, channel] = base + glow * (2, 10, 13)[channel] + secondary * (4, 3, 4)[channel]
    # Reproducible dither prevents visible gradient banding without noisy grain.
    noise = np.random.default_rng(37).uniform(-0.6, 0.6, (height, width, 1))
    canvas = np.uint8(np.clip(canvas + noise, 0, 255))
    image = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(image)
    for x in range(80, width, 80):
        for y in range(80, height, 80):
            draw.ellipse((x, y, x + 1, y + 1), fill=(26, 43, 59))
    return image


def brand_mark(draw: ImageDraw.ImageDraw, x: int, y: int, scale: float = 1.0) -> None:
    # Geometry and colors from frontend/public/favicon.svg (viewBox 0 0 32 32).
    unit = scale * 36 / 32
    draw.rounded_rectangle((x, y, x + 32 * unit, y + 32 * unit), radius=7 * unit, fill=(16, 23, 32))
    points = [(9, 9), (23, 9), (23, 15), (15, 15), (15, 23), (9, 23), (9, 9)]
    pts = [(x + px * unit, y + py * unit) for px, py in points]
    draw.line(pts, fill=(131, 183, 250), width=max(2, round(3 * unit)), joint="curve")
    draw.ellipse((x + 20 * unit, y + 20 * unit, x + 26 * unit, y + 26 * unit), fill=(131, 183, 250))


def top_bar(draw: ImageDraw.ImageDraw, scene: dict[str, Any], index: int, total: int) -> None:
    brand_mark(draw, 65, 45, 0.83)
    draw.text((111, 44), "FRAUDLOOP", font=font(24, "bold"), fill=WHITE)
    draw.line((299, 48, 299, 73), fill=LINE, width=1)
    draw.text((320, 43), str(scene.get("chapter", "Product walkthrough")).upper(), font=font(24, "medium"), fill=CYAN)
    label = f"{index + 1:02d}  /  {total:02d}"
    draw.text((1848 - font(24, "mono").getlength(label), 43), label, font=font(24, "mono"), fill=MUTED)


def footer(draw: ImageDraw.ImageDraw, scene: dict[str, Any], index: int, total: int) -> None:
    # Stable chapter progress stays outside the application image.
    gap = 8
    start_x, end_x = 64, 1856
    segment_width = (end_x - start_x - gap * (total - 1)) / total
    for part in range(total):
        left = start_x + part * (segment_width + gap)
        color = CYAN if part == index else ((58, 100, 112) if part < index else (29, 43, 59))
        draw.rounded_rectangle((left, 1016, left + segment_width, 1020), radius=1, fill=color)
    label = str(scene.get("footer", "TigerGraph HHGOA  •  Product demonstration"))
    text_block(draw, label, (64, 1033), 1792, size=22, max_lines=1, fill=(144, 166, 185))


def load_asset(scene: dict[str, Any], manifest_dir: Path) -> Image.Image | None:
    if not scene.get("asset"):
        return None
    source_path = (manifest_dir / scene["asset"]).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Missing asset for {scene['id']}: {source_path}")
    with Image.open(source_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    focus = scene.get("focus")
    if focus is not None:
        if not isinstance(focus, list) or len(focus) != 4:
            raise ValueError(f"{scene['id']}: focus must be [x, y, width, height]")
        x, y, width, height = map(float, focus)
        if all(0 <= value <= 1 for value in focus):
            x, width = x * image.width, width * image.width
            y, height = y * image.height, height * image.height
        if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > image.width + 1 or y + height > image.height + 1:
            raise ValueError(f"{scene['id']}: focus lies outside the source image")
        image = image.crop((round(x), round(y), round(x + width), round(y + height)))
    return image


def window(
    canvas: Image.Image,
    screenshot: Image.Image,
    box: tuple[int, int, int, int],
    scene: dict[str, Any],
) -> None:
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    shadow = Image.new("RGBA", BASE_SIZE, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((left - 4, top + 20, right + 4, bottom + 20), radius=22, fill=(0, 0, 0, 125))
    blurred_shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    canvas.paste(blurred_shadow, (0, 0), blurred_shadow)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, radius=18, fill=(10, 19, 32), outline=(55, 78, 99), width=1)
    draw.rounded_rectangle((left + 1, top + 1, right - 1, top + 46), radius=17, fill=(20, 34, 51))
    draw.rectangle((left + 1, top + 25, right - 1, top + 46), fill=(20, 34, 51))
    for index, color in enumerate(((145, 81, 78), (160, 132, 75), (67, 128, 123))):
        cx = left + 24 + index * 18
        draw.ellipse((cx - 4, top + 19, cx + 4, top + 27), fill=color)
    chrome_label = str(scene.get("url", "FRAUDLOOP  /  Investigation console"))
    chrome_font = font(16)
    label_width = chrome_font.getlength(chrome_label)
    draw.text((left + (width - label_width) / 2, top + 11), chrome_label, font=chrome_font, fill=(143, 166, 185))
    content_box = (width - 20, height - 57)
    fitted = ImageOps.contain(screenshot, content_box, Image.Resampling.LANCZOS)
    x = left + (width - fitted.width) // 2
    y = top + 48 + (content_box[1] - fitted.height) // 2
    mask = Image.new("L", fitted.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fitted.width - 1, fitted.height - 1), radius=5, fill=255)
    canvas.paste(fitted, (x, y), mask)
    camera_image = fitted.convert("RGBA")
    camera_image.putalpha(mask)
    scene["_camera"] = {"image": camera_image, "x": x, "y": y, "width": fitted.width, "height": fitted.height}


def badge(draw: ImageDraw.ImageDraw, text: str, x: int, y: int, color: tuple[int, int, int] = CYAN) -> int:
    selected_font = font(24, "medium")
    width = round(selected_font.getlength(text)) + 30
    draw.rounded_rectangle((x, y, x + width, y + 44), radius=22, fill=(19, 39, 52), outline=(40, 76, 88), width=1)
    draw.text((x + 15, y + 5), text, font=selected_font, fill=color)
    return width


def graph_motif(image: Image.Image) -> None:
    """Decorative identity graphic; deliberately not presented as case evidence."""
    draw = ImageDraw.Draw(image)
    nodes = [(1455, 328), (1685, 405), (1776, 650), (1570, 794), (1340, 706), (1270, 496), (1530, 562)]
    edges = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0), (0, 6), (1, 6), (2, 6), (3, 6), (4, 6), (5, 6)]
    for first, second in edges:
        draw.line((nodes[first], nodes[second]), fill=(28, 73, 86), width=2)
    for index, (x, y) in enumerate(nodes):
        radius = 18 if index == 6 else 10
        draw.ellipse((x - radius - 10, y - radius - 10, x + radius + 10, y + radius + 10), outline=(26, 64, 78), width=1)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=CYAN if index == 6 else (58, 119, 133))


def hero_scene(image: Image.Image, scene: dict[str, Any], *, end: bool = False) -> None:
    graph_motif(image)
    draw = ImageDraw.Draw(image)
    badge(draw, str(scene.get("badge", "Evidence. Policy. Human judgment.")), 80, 165, AMBER if end else CYAN)
    height = text_block(draw, scene["title"], (80, 286), 1130, size=102 if not end else 91, minimum=76, weight="bold", max_lines=3, leading=1.16)
    text_block(draw, scene["caption"], (85, 322 + height), 1070, size=32, max_lines=4, fill=MUTED, leading=1.5)
    draw.line((85, 888, 1100, 888), fill=LINE, width=1)
    phrases = scene.get("tags", ["GRAPH EVIDENCE", "GUARDED AI", "HUMAN REVIEW"])
    left = 85
    for phrase in phrases[:3]:
        draw.text((left, 920), str(phrase), font=font(24, "medium"), fill=CYAN)
        left += font(24, "medium").getlength(str(phrase)) + 65


def product_scene(image: Image.Image, scene: dict[str, Any], screenshot: Image.Image) -> None:
    layout = scene.get("layout", "auto")
    wide = layout == "wide" or (layout == "auto" and screenshot.width / screenshot.height >= 2.12)
    draw = ImageDraw.Draw(image)
    if wide:
        text_block(draw, scene["title"], (64, 101), 1755, size=55, minimum=45, weight="bold", max_lines=1, leading=1.2)
        window(image, screenshot, (64, 198, 1856, 901), scene)
        draw = ImageDraw.Draw(image)
        text_block(draw, scene["caption"], (79, 915), 1740, size=28, minimum=26, max_lines=2, fill=MUTED, leading=1.4)
    else:
        window(image, screenshot, (548, 133, 1856, 985), scene)
        draw = ImageDraw.Draw(image)
        if scene.get("badge"):
            badge(draw, str(scene["badge"]), 64, 174)
        title_y = 250 if scene.get("badge") else 218
        title_height = text_block(draw, scene["title"], (64, title_y), 430, size=58, minimum=45, weight="bold", max_lines=5, leading=1.19)
        caption_y = title_y + title_height + 34
        caption_height = text_block(draw, scene["caption"], (68, caption_y), 420, size=29, minimum=27, max_lines=8, fill=MUTED, leading=1.47)
        if caption_y + caption_height > 969:
            raise ValueError(f"{scene['id']}: split title and caption are too tall; shorten copy or use a wide focus crop")
        draw.line((68, 971, 192, 971), fill=CYAN, width=3)


def proof_scene(image: Image.Image, scene: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(image)
    text_block(draw, scene["title"], (80, 167), 1650, size=73, minimum=58, weight="bold", max_lines=2, leading=1.18)
    if scene.get("badge"):
        badge(draw, str(scene["badge"]), 85, 335, AMBER)
    stats = scene.get("stats", [])
    if not stats:
        raise ValueError(f"{scene['id']}: a proof scene needs stats or an asset")
    if len(stats) > 4:
        raise ValueError(f"{scene['id']}: proof scenes support up to four stats")
    gap = 30
    left = 80
    card_width = int((1760 - (len(stats) - 1) * gap) / len(stats))
    for number, stat in enumerate(stats):
        x = left + number * (card_width + gap)
        draw.rounded_rectangle((x, 446, x + card_width, 727), radius=17, fill=PANEL, outline=LINE, width=1)
        draw.line((x + 28, 446, x + 112, 446), fill=CYAN if number != len(stats) - 1 else AMBER, width=3)
        text_block(draw, str(stat["value"]), (x + 28, 480), card_width - 56, size=92, minimum=62, weight="mono", max_lines=1, fill=WHITE)
        text_block(draw, str(stat["label"]), (x + 30, 628), card_width - 58, size=26, minimum=22, max_lines=2, fill=MUTED, leading=1.35)
    text_block(draw, scene["caption"], (85, 803), 1690, size=31, max_lines=3, fill=MUTED, leading=1.45)


def render_frame(scene: dict[str, Any], index: int, total: int, manifest_dir: Path, base: Image.Image) -> Image.Image:
    image = base.copy()
    screenshot = load_asset(scene, manifest_dir)
    kind = scene.get("type", "product")
    if kind in {"hero", "end"}:
        hero_scene(image, scene, end=kind == "end")
    elif screenshot is not None:
        product_scene(image, scene, screenshot)
    elif kind == "proof":
        proof_scene(image, scene)
    else:
        raise ValueError(f"{scene['id']}: product scenes require a local image asset")
    draw = ImageDraw.Draw(image)
    top_bar(draw, scene, index, total)
    footer(draw, scene, index, total)
    return image


def validate_manifest(path: Path, fps: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    scenes = payload.get("scenes") if isinstance(payload, dict) else payload
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("Manifest must be a nonempty array or an object containing scenes")
    seen: set[str] = set()
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f"Scene {index + 1} is not an object")
        for key in ("id", "title", "caption", "duration"):
            if key not in scene:
                raise ValueError(f"Scene {index + 1} is missing {key}")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", scene["id"]):
            raise ValueError("Scene IDs may contain only letters, numbers, underscores, and hyphens")
        if scene["id"] in seen:
            raise ValueError(f"Duplicate scene ID: {scene['id']}")
        seen.add(scene["id"])
        if scene.get("type", "product") not in {"hero", "product", "proof", "end"}:
            raise ValueError(f"Unsupported scene type: {scene.get('type')}")
        if scene.get("layout", "auto") not in {"auto", "wide", "split"}:
            raise ValueError(f"Unsupported scene layout: {scene.get('layout')}")
        duration = float(scene["duration"])
        if not math.isfinite(duration) or duration < 1 or duration > 120:
            raise ValueError("Each scene duration must be between 1 and 120 seconds")
        # All artifact timings use actual integer video frames.
        scene["frames"] = round(duration * fps)
        scene["duration"] = scene["frames"] / fps
    return scenes


def timecode(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def metadata_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")


def write_text_artifacts(scenes: list[dict[str, Any]], out_dir: Path, output_stem: str) -> tuple[Path, Path]:
    subtitle_file = out_dir / f"{output_stem}.srt"
    metadata_file = out_dir / "chapters.ffmetadata"
    subtitles, chapters = [], [";FFMETADATA1", "title=FRAUDLOOP — Product walkthrough", "artist=FRAUDLOOP", "comment=Local application captures with explanatory captions. No external bank actions executed."]
    elapsed = 0.0
    for index, scene in enumerate(scenes):
        duration = scene["duration"]
        subtitles.append(f"{index + 1}\n{timecode(elapsed + .15)} --> {timecode(elapsed + duration - .15)}\n{scene['caption']}\n")
        chapters.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(elapsed * 1000)}", f"END={round((elapsed + duration) * 1000)}", f"title={metadata_escape(scene['title'])}"])
        elapsed += duration
    subtitle_file.write_text("\n".join(subtitles))
    metadata_file.write_text("\n".join(chapters) + "\n")
    return subtitle_file, metadata_file


def contact_sheet(frames: list[Path], scenes: list[dict[str, Any]], out_dir: Path) -> None:
    columns = min(4, len(frames))
    thumb_width, thumb_height = 480, 270
    gap, label_height = 18, 51
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (columns * (thumb_width + gap) + gap, 80 + rows * (thumb_height + label_height + gap)), INK)
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 19), "FRAUDLOOP  /  EDITORIAL CONTACT SHEET", font=font(25, "bold"), fill=WHITE)
    elapsed = 0.0
    thumb_dir = out_dir / "thumbnails"
    thumb_dir.mkdir(exist_ok=True)
    for index, (path, scene) in enumerate(zip(frames, scenes, strict=True)):
        with Image.open(path) as opened:
            thumb = opened.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        thumb.save(thumb_dir / f"{index + 1:02}-{scene['id']}.jpg", quality=92)
        x = gap + (index % columns) * (thumb_width + gap)
        y = 76 + (index // columns) * (thumb_height + label_height + gap)
        sheet.paste(thumb, (x, y))
        label = f"{index + 1:02}  {timecode(elapsed)[:-4]}  ·  {scene['duration']:g}s"
        draw.text((x + 3, y + thumb_height + 6), label, font=font(17, "mono"), fill=CYAN)
        draw.text((x + 3, y + thumb_height + 28), str(scene.get("chapter", scene["id"])), font=font(16), fill=MUTED)
        elapsed += scene["duration"]
    sheet.save(out_dir / "contact-sheet.png")
    with Image.open(frames[0]) as poster:
        poster.save(out_dir / "poster.jpg", quality=95)


def make_transition_audio(scenes: list[dict[str, Any]], path: Path, rate: int = 48_000) -> None:
    duration = sum(scene["duration"] for scene in scenes)
    mix = np.zeros((round(duration * rate), 2), dtype=np.float32)
    elapsed = 0.0
    for index, scene in enumerate(scenes):
        count = round(rate * (0.7 if index == 0 else 0.38))
        t = np.arange(count, dtype=np.float64) / rate
        envelope = np.sin(np.pi * np.arange(count) / count) ** 2 * np.exp(-t * 5)
        pitch = 440.0 if index % 2 == 0 else 392.0
        signal = (np.sin(2 * np.pi * pitch * t) + .36 * np.sin(2 * np.pi * pitch * 1.5 * t) + .12 * np.sin(2 * np.pi * pitch * 2 * t)) * envelope * .037
        start = round((elapsed + .16) * rate)
        stop = min(start + count, len(mix))
        mix[start:stop, 0] += signal[:stop - start] * .92
        mix[start:stop, 1] += signal[:stop - start]
        elapsed += scene["duration"]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes())


def run_command(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"Media rendering failed:\n{completed.stderr[-8000:]}")


def encode_segment(frame: Path, target: Path, scene: dict[str, Any], args: argparse.Namespace) -> None:
    cache_key = hashlib.sha256(frame.read_bytes() + json.dumps({"fps": args.fps, "frames": scene["frames"], "size": args.size, "renderer": 4}, sort_keys=True).encode()).hexdigest()
    cache_file = target.with_suffix(".sha256")
    if not args.force and target.is_file() and cache_file.is_file() and cache_file.read_text().strip() == cache_key:
        print(f"  Reusing {target.name}", flush=True)
        return
    fade_in = min(.32, scene["duration"] / 5)
    fade_out = min(.23, scene["duration"] / 5)
    slide = max(6, round(args.height / 1080 * 16))
    # Loop filters cache decoded stills. Text/chrome stays fixed, while product
    # screenshots gently grow 1.7% to their full contained size, without cropping.
    timing = f"loop=loop=-1:size=1:start=0,settb=1/{args.fps},setpts=N"
    filters = (
        f"pad=iw:ih+{slide}:0:{slide}:color=0x070e1b,"
        f"crop=iw:ih-{slide}:0:'{slide}*(1-pow(1-min(t/0.85,1),3))',"
        f"fade=t=in:st=0:d={fade_in}:color=0x070e1b,"
        f"fade=t=out:st={scene['duration'] - fade_out}:d={fade_out}:color=0x070e1b,"
        "format=yuv420p"
    )
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    camera = scene.get("_camera_files")
    if camera:
        progress = f"min(t/{scene['duration']},1)"
        zoom = f"(0.983+0.017*{progress})"
        camera_filter = (
            f"[0:v]{timing},format=rgba[base];"
            f"[1:v]{timing},scale=w='iw*{zoom}':h='ih*{zoom}':eval=frame:flags=bicubic[shot];"
            f"[base][shot]overlay=x='{camera['center_x']}-overlay_w/2':y='{camera['center_y']}-overlay_h/2':eval=frame:format=auto,"
            f"{filters}[result]"
        )
        command += ["-i", camera["base"], "-i", camera["shot"], "-filter_complex", camera_filter, "-map", "[result]"]
    else:
        command += ["-i", str(frame), "-vf", f"{timing},{filters}"]
    command += ["-frames:v", str(scene["frames"]), "-r", str(args.fps), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", str(args.threads), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
    run_command(command)
    cache_file.write_text(cache_key + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "scenes.json")
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "fraudloop-demo.mp4")
    parser.add_argument("--preview", action="store_true", help="Produce frames, thumbnails, contact sheet, and captions without video encoding")
    parser.add_argument("--size", default="1920x1080", help="Landscape output resolution, e.g. 1280x720")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--no-audio", action="store_true", help="Omit original, softly synthesized transition sounds")
    parser.add_argument("--force", action="store_true", help="Re-encode scenes even if their source frames are unchanged")
    args = parser.parse_args()
    try:
        args.width, args.height = map(int, args.size.lower().split("x"))
        if args.width % 2 or args.height % 2 or args.width < 640 or args.height < 360 or abs(args.width / args.height - 16 / 9) > .001:
            parser.error("--size must be even dimensions at 16:9, at least 640x360")
        if args.fps < 15 or args.fps > 60 or args.threads < 1:
            parser.error("Use 15–60 fps and at least one encoder thread")
        args.manifest = args.manifest.resolve()
        args.output = args.output.resolve()
        out_dir = args.output.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        frame_dir = out_dir / "frames"
        frame_dir.mkdir(exist_ok=True)
        scenes = validate_manifest(args.manifest, args.fps)
        print(f"Preparing {len(scenes)} scenes / {sum(s['duration'] for s in scenes):.2f} seconds", flush=True)
        base = background()
        frames = []
        for index, scene in enumerate(scenes):
            rendered = render_frame(scene, index, len(scenes), args.manifest.parent, base)
            camera = scene.pop("_camera", None)
            if camera:
                camera_base = rendered.copy()
                x, y, width, height = (camera[key] for key in ("x", "y", "width", "height"))
                ImageDraw.Draw(camera_base).rectangle((x, y, x + width - 1, y + height - 1), fill=(10, 19, 32))
                camera_shot = camera["image"]
                output_scale = args.width / BASE_SIZE[0]
                if output_scale != 1:
                    camera_base = camera_base.resize((args.width, args.height), Image.Resampling.LANCZOS)
                    camera_shot = camera_shot.resize((round(width * output_scale), round(height * output_scale)), Image.Resampling.LANCZOS)
                base_path = frame_dir / f"{index + 1:02}-{scene['id']}-base.png"
                shot_path = frame_dir / f"{index + 1:02}-{scene['id']}-camera.png"
                camera_base.save(base_path, compress_level=3)
                camera_shot.save(shot_path, compress_level=3)
                scene["_camera_files"] = {"base": str(base_path), "shot": str(shot_path), "center_x": (x + width / 2) * output_scale, "center_y": (y + height / 2) * output_scale}
            if (args.width, args.height) != BASE_SIZE:
                rendered = rendered.resize((args.width, args.height), Image.Resampling.LANCZOS)
            path = frame_dir / f"{index + 1:02}-{scene['id']}.png"
            rendered.save(path, compress_level=3)
            frames.append(path)
        subtitles, metadata = write_text_artifacts(scenes, out_dir, args.output.stem)
        contact_sheet(frames, scenes, out_dir)
        if args.preview:
            print(f"Preview ready: {out_dir / 'contact-sheet.png'}", flush=True)
            return 0
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required for video encoding; preview artifacts are ready")
        segment_dir = out_dir / "segments"
        segment_dir.mkdir(exist_ok=True)
        segments = []
        for index, (frame, scene) in enumerate(zip(frames, scenes, strict=True)):
            target = segment_dir / f"{index + 1:02}-{scene['id']}.mp4"
            print(f"Encoding scene {index + 1}/{len(scenes)}: {scene['title']}", flush=True)
            encode_segment(frame, target, scene, args)
            segments.append(target)
        concat_file = out_dir / "segments.txt"
        concat_file.write_text("".join("file '" + str(path).replace("'", "'\\''") + "'\n" for path in segments))
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file)]
        if not args.no_audio:
            audio_file = out_dir / "transition-sounds.wav"
            make_transition_audio(scenes, audio_file)
            command += ["-i", str(audio_file)]
        subtitle_index = 1 if args.no_audio else 2
        metadata_index = subtitle_index + 1
        command += ["-i", str(subtitles), "-i", str(metadata), "-map", "0:v:0"]
        if not args.no_audio:
            command += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k"]
        command += ["-map", f"{subtitle_index}:s:0", "-map_metadata", str(metadata_index), "-map_chapters", str(metadata_index), "-c:v", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English captions", "-disposition:s:0", "0", "-movflags", "+faststart", str(args.output)]
        print("Assembling final MP4, chapters, and optional captions", flush=True)
        run_command(command)
        provenance = {
            "manifest": str(args.manifest), "resolution": args.size, "fps": args.fps,
            "duration_seconds": sum(scene["duration"] for scene in scenes),
            "scenes": len(scenes), "audio": "Original synthesized transition tones; no voiceover or third-party music" if not args.no_audio else "None",
            "visuals": "Local application screenshots and locally drawn titles; screenshots only cropped where manifest focus is explicit",
            "output": str(args.output), "subtitles": str(subtitles),
        }
        (out_dir / "render-info.json").write_text(json.dumps(provenance, indent=2) + "\n")
        print(f"Ready: {args.output}", flush=True)
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Render error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
