#!/usr/bin/env python3
"""Add locally generated narration while copying the original video packets.

This offline assembler never reads credentials or makes network requests.
Voice files are <scene-id>.mp3 under --voice-dir. Optional <scene-id>.alignment.json
or <scene-id>.json files may contain ElevenLabs character alignment timestamps.
"""

from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
import wave


ROOT = Path(__file__).resolve().parent
RATE = 48_000


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Media operation failed:\n{result.stderr[-7000:]}")
    return result


def ffmpeg(*arguments: str) -> subprocess.CompletedProcess[str]:
    return run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *arguments])


def probe(path: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-show_chapters", "-of", "json", str(path)]).stdout)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(temporary, path)


def atomic_copy(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def manifest_scenes(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    scenes = payload.get("scenes") if isinstance(payload, dict) else payload
    if not isinstance(scenes, list) or not scenes:
        raise ValueError(f"{path.name} must contain a nonempty scenes array")
    ids = set()
    for scene in scenes:
        if not isinstance(scene, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(scene.get("id", ""))):
            raise ValueError(f"Invalid scene in {path.name}")
        if scene["id"] in ids:
            raise ValueError(f"Duplicate scene ID: {scene['id']}")
        ids.add(scene["id"])
        duration = float(scene.get("duration", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"Invalid duration for {scene['id']}")
        scene["duration"] = duration
    return scenes


def decoded_audio(path: Path, target: Path) -> tuple[array, float]:
    ffmpeg("-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(target))
    with wave.open(str(target), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getframerate() != RATE or audio.getsampwidth() != 2:
            raise ValueError(f"Unexpected decoded audio format for {path.name}")
        samples = array("h", audio.readframes(audio.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples, len(samples) / RATE


def edge_silence(samples: array) -> tuple[float, float]:
    """Only remove edge samples below -55 dBFS, retaining 80/120 ms guards.

    The conservative sample threshold preserves quiet consonants and breaths.
    Nothing between the first and last active sample is removed.
    """
    threshold = 32767 * 10 ** (-55 / 20)
    first = next((index for index, value in enumerate(samples) if abs(value) > threshold), None)
    if first is None:
        raise ValueError("Voice file contains no audible speech above -55 dBFS")
    last = next(index for index in range(len(samples) - 1, -1, -1) if abs(samples[index]) > threshold)
    start = max(0, first - round(.080 * RATE))
    end = min(len(samples), last + 1 + round(.120 * RATE))
    return start / RATE, end / RATE


def alignment_for(scene: dict, voice_dir: Path, raw_duration: float) -> tuple[list[dict] | None, str | None]:
    candidates = [voice_dir / f"{scene['id']}.alignment.json", voice_dir / f"{scene['id']}.json"]
    if scene.get("alignment"):
        candidates.insert(0, (voice_dir / scene["alignment"]).resolve())
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return None, None
    payload = json.loads(path.read_text())
    selected = payload.get("alignment") or payload.get("normalized_alignment") or payload
    characters = selected.get("characters", [])
    starts = selected.get("character_start_times_seconds", [])
    ends = selected.get("character_end_times_seconds", [])
    if not characters or len(characters) != len(starts) or len(starts) != len(ends):
        raise ValueError(f"Invalid character alignment: {path.name}")
    if not all(isinstance(character, str) and len(character) == 1 for character in characters):
        raise ValueError(f"Alignment must use individual characters: {path.name}")
    previous = -1.0
    for start, end in zip(starts, ends, strict=True):
        if not all(math.isfinite(float(value)) for value in (start, end)) or float(start) < 0 or float(end) < float(start) or float(start) < previous - .025 or float(end) > raw_duration + .20:
            raise ValueError(f"Invalid alignment timestamp in {path.name}")
        previous = float(start)
    text = "".join(characters)
    words = [{"text": match.group(), "start": float(starts[match.start()]), "end": float(ends[match.end() - 1])} for match in re.finditer(r"\S+", text)]
    if not words:
        raise ValueError(f"No words in alignment: {path.name}")
    return words, str(path)


def fit_audio(decoded: Path, output: Path, row: dict, max_tempo: float) -> None:
    tempo = row["tempo"]
    for attempt in range(3):
        filters = f"atrim=start={row['trim_start']:.8f}:end={row['trim_end']:.8f},asetpts=PTS-STARTPTS"
        if tempo > 1.000001:
            filters += f",atempo={tempo:.8f}"
        ffmpeg("-loglevel", "error", "-i", str(decoded), "-af", filters, "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(output))
        with wave.open(str(output), "rb") as audio:
            actual = audio.getnframes() / audio.getframerate()
        if actual <= row["available_seconds"] + .005:
            row["tempo"] = tempo
            row["rendered_duration"] = actual
            row["speech_end"] = row["speech_start"] + actual
            row["remaining_scene_gap"] = row["scene_end"] - row["speech_end"]
            return
        proposed = tempo * actual / row["available_seconds"] * 1.001
        if proposed > max_tempo + .000001:
            raise ValueError(f"{row['id']} still exceeds its slot after tempo adjustment; shorten or regenerate its narration")
        tempo = proposed
    raise ValueError(f"Unable to fit {row['id']} without clipping speech")


def assemble_timeline(rows: list[dict], processed: list[Path], duration: float, output: Path) -> None:
    timeline = array("h", [0]) * round(duration * RATE)
    for row, path in zip(rows, processed, strict=True):
        with wave.open(str(path), "rb") as audio:
            samples = array("h", audio.readframes(audio.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        start = round(row["speech_start"] * RATE)
        end = start + len(samples)
        if end > len(timeline) or end > round((row["scene_end"] - .20) * RATE):
            raise ValueError(f"Narration exceeds its scene: {row['id']}")
        timeline[start:end] = samples
    if sys.byteorder != "little":
        timeline.byteswap()
    with wave.open(str(output), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(timeline.tobytes())


def loudness_json(stderr: str) -> dict:
    matches = re.findall(r'\{\s*"input_i"[\s\S]*?\}', stderr)
    if not matches:
        raise RuntimeError("FFmpeg did not return loudness measurements")
    result = json.loads(matches[-1])
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if not math.isfinite(float(result[key])):
            raise ValueError("Audio is silent or has invalid loudness measurements")
    return result


def measure_loudness(path: Path, prefix: str = "", target_peak: float = -2.0) -> dict:
    graph = f"{prefix}loudnorm=I=-16:TP={target_peak}:LRA=11:print_format=json"
    return loudness_json(ffmpeg("-i", str(path), "-map", "0:a:0", "-af", graph, "-f", "null", "-").stderr)


def normalize_audio(source: Path, output: Path, *, stereo_from_mono: bool = False, target_peak: float = -2.0) -> dict:
    prefix = "pan=stereo|c0=0.7071067812*c0|c1=0.7071067812*c0," if stereo_from_mono else ""
    measured = measure_loudness(source, prefix, target_peak)
    filters = prefix + f"loudnorm=I=-16:TP={target_peak}:LRA=11:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true:print_format=json"
    result = ffmpeg("-i", str(source), "-af", filters, "-ar", str(RATE), "-ac", "2", "-c:a", "pcm_s24le", str(output))
    return loudness_json(result.stderr)


def chunks_from_words(words: list[dict]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        proposed = " ".join(item["text"] for item in current + [word])
        lines = textwrap.wrap(proposed, width=44, break_long_words=False, break_on_hyphens=False)
        if current and (len(lines) > 2 or word["end"] - current[0]["start"] > 5.2):
            chunks.append(current)
            current = []
        current.append(word)
        if len(current) >= 4 and re.search(r"[.!?;:]$", word["text"]):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def subtitle_cues(scene: dict, row: dict, words: list[dict] | None) -> list[dict]:
    cues = []
    if words is None:
        plain_words = scene["text"].split()
        weights = [max(1, len(re.sub(r"\W", "", word))) for word in plain_words]
        total_weight = sum(weights)
        elapsed = 0.0
        words = []
        for word, weight in zip(plain_words, weights, strict=True):
            start = row["trim_start"] + elapsed * row["trimmed_duration"] / total_weight
            elapsed += weight
            end = row["trim_start"] + elapsed * row["trimmed_duration"] / total_weight
            words.append({"text": word, "start": start, "end": end})
    for group in chunks_from_words(words):
        start = row["speech_start"] + max(0.0, (group[0]["start"] - row["trim_start"]) / row["tempo"])
        end = row["speech_start"] + max(0.0, (group[-1]["end"] - row["trim_start"]) / row["tempo"]) + .08
        start = min(max(start, row["speech_start"]), row["speech_end"] - .08)
        end = min(max(end, start + .08), row["speech_end"])
        text = " ".join(word["text"] for word in group)
        cues.append({"start": start, "end": end, "text": "\n".join(textwrap.wrap(text, width=44, break_long_words=False, break_on_hyphens=False))})
    for index in range(len(cues) - 1):
        cues[index]["end"] = min(cues[index]["end"], cues[index + 1]["start"] - .001)
        if cues[index]["end"] <= cues[index]["start"]:
            raise ValueError(f"Invalid subtitle timing for {row['id']}")
    return cues


def timecode(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def write_srt(cues: list[dict], output: Path) -> None:
    output.write_text("\n".join(f"{index + 1}\n{timecode(cue['start'])} --> {timecode(cue['end'])}\n{cue['text']}\n" for index, cue in enumerate(cues)))


def packet_signature(path: Path, selector: str = "v:0") -> tuple[str, int]:
    result = run(["ffprobe", "-v", "error", "-select_streams", selector, "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=pts,dts,duration,size,data_hash", "-of", "json", str(path)])
    packets = json.loads(result.stdout).get("packets", [])
    if not packets:
        raise ValueError(f"No packets found for {selector} in {path.name}")
    digest = hashlib.sha256(json.dumps(packets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest, len(packets)


def mux(source: Path, audio: Path, subtitles: Path, output: Path, original_subtitles: int) -> None:
    command = ["-loglevel", "error", "-i", str(source), "-i", str(audio), "-i", str(subtitles), "-map", "0:v:0", "-map", "1:a:0", "-map", "0:s?", "-map", "2:s:0", "-map_metadata", "0", "-map_chapters", "0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "copy", f"-c:s:{original_subtitles}", "mov_text", "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=English narration with original demo sounds", f"-metadata:s:s:{original_subtitles}", "language=eng", f"-metadata:s:s:{original_subtitles}", "title=English narration", f"-disposition:s:{original_subtitles}", "0", "-movflags", "+faststart", str(output)]
    ffmpeg(*command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "out/fraudloop-demo.mp4")
    parser.add_argument("--manifest", type=Path, default=ROOT / "narration.json")
    parser.add_argument("--scenes", type=Path, default=ROOT / "scenes.json")
    parser.add_argument("--voice-dir", type=Path, default=ROOT / "out/voiceover")
    parser.add_argument("--output", type=Path, default=ROOT / "out/fraudloop-demo-narrated.mp4")
    parser.add_argument("--max-tempo", type=float, default=1.18)
    parser.add_argument("--offset", type=float, default=.30)
    parser.add_argument("--end-gap", type=float, default=.30)
    parser.add_argument("--sfx-gain", type=float, default=.28)
    parser.add_argument("--dry-run", action="store_true", help="Decode and inspect timing; do not assemble or export the narrated video")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing narrated output, never the original")
    args = parser.parse_args()
    temporary_output: Path | None = None
    try:
        for attribute in ("source", "manifest", "scenes", "voice_dir", "output"):
            setattr(args, attribute, getattr(args, attribute).resolve())
        if args.source == args.output or (args.output.exists() and os.path.samefile(args.source, args.output)):
            raise ValueError("Source and narrated output must be different files")
        if args.output.exists() and not args.overwrite and not args.dry_run:
            raise ValueError("Narrated output already exists; use --overwrite to replace only that output")
        if not (1 <= args.max_tempo <= 1.18) or min(args.offset, args.end_gap) < .20 or not (0 <= args.sfx_gain <= 1):
            raise ValueError("Use max tempo 1–1.18, edge gaps of at least 0.20 seconds, and SFX gain 0–1")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg and ffprobe must be installed")
        scenes = manifest_scenes(args.manifest)
        visual_scenes = manifest_scenes(args.scenes)
        if [(s["id"], s["duration"]) for s in scenes] != [(s["id"], s["duration"]) for s in visual_scenes]:
            raise ValueError("Narration IDs, order, and durations must exactly match the original visual scenes")
        for scene in scenes:
            if not isinstance(scene.get("text"), str) or not scene["text"].strip():
                raise ValueError(f"Missing narration text for {scene['id']}")
        source_probe = probe(args.source)
        duration = sum(scene["duration"] for scene in scenes)
        if abs(float(source_probe["format"]["duration"]) - duration) > .05:
            raise ValueError("Scene durations do not match the original video; visual retiming is prohibited")
        missing = [scene["id"] for scene in scenes if not (args.voice_dir / f"{scene['id']}.mp3").is_file()]
        if missing:
            raise FileNotFoundError("Missing generated voice files: " + ", ".join(missing))
        source_hash = file_hash(args.source)
        args.voice_dir.mkdir(parents=True, exist_ok=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report = {"source": str(args.source), "source_sha256": source_hash, "output": str(args.output), "total_duration": duration, "scene_count": len(scenes), "max_tempo": args.max_tempo, "silence_trim": "Only edge samples below -55 dBFS; 80 ms leading and 120 ms trailing guards retained; internal pauses preserved", "sfx_gain": args.sfx_gain, "dry_run": args.dry_run, "scenes": []}
        with tempfile.TemporaryDirectory(prefix="fraudloop-narration-") as temporary:
            work = Path(temporary)
            decoded_files, alignments = [], []
            elapsed = 0.0
            overlong = []
            for scene in scenes:
                voice = args.voice_dir / f"{scene['id']}.mp3"
                voice_hash = file_hash(voice)
                decoded = work / f"{scene['id']}-decoded.wav"
                samples, raw_duration = decoded_audio(voice, decoded)
                trim_start, trim_end = edge_silence(samples)
                del samples
                words, alignment_path = alignment_for(scene, args.voice_dir, raw_duration)
                if file_hash(voice) != voice_hash:
                    raise ValueError(f"Voice file changed while decoding: {scene['id']}; wait for generation to finish")
                trimmed = trim_end - trim_start
                available = scene["duration"] - args.offset - args.end_gap
                if available <= 0:
                    raise ValueError(f"No narration space in {scene['id']}")
                tempo = max(1.0, trimmed / available)
                row = {"id": scene["id"], "scene_start": elapsed, "scene_end": elapsed + scene["duration"], "slot_duration": scene["duration"], "raw_duration": raw_duration, "trim_start": trim_start, "trim_end": trim_end, "trimmed_duration": trimmed, "available_seconds": available, "tempo": tempo, "speech_start": elapsed + args.offset, "speech_end": elapsed + args.offset + trimmed / tempo, "alignment": alignment_path, "alignment_sha256": file_hash(Path(alignment_path)) if alignment_path else None, "caption_timing": "ElevenLabs character timestamps" if words else "Proportional scene timing; no character alignment supplied", "source_audio_sha256": voice_hash}
                report["scenes"].append(row)
                decoded_files.append(decoded)
                alignments.append(words)
                print(f"{scene['id']}: {trimmed:.2f}s speech / {available:.2f}s slot / {tempo:.3f}x", flush=True)
                if tempo > args.max_tempo + .000001:
                    overlong.append(f"{scene['id']} needs {tempo:.3f}x; allowed audio length is {available * args.max_tempo:.2f}s after edge silence")
                elapsed += scene["duration"]
            report["timing_passed"] = not overlong
            atomic_json(args.voice_dir / "timing-report.json", report)
            if overlong:
                raise ValueError("Regenerate shorter or faster narration:\n" + "\n".join(overlong))
            if args.dry_run:
                print(f"Timing passed for all {len(scenes)} scenes. No video or audio master exported.", flush=True)
                return 0
            processed = []
            cues = []
            for scene, row, decoded, words in zip(scenes, report["scenes"], decoded_files, alignments, strict=True):
                path = work / f"{scene['id']}-fitted.wav"
                fit_audio(decoded, path, row, args.max_tempo)
                processed.append(path)
                cues.extend(subtitle_cues(scene, row, words))
            raw_timeline = work / "narration-aligned.wav"
            master = work / "narration-master.wav"
            mixed = work / "mixed.wav"
            mix_master = work / "narration-mix.wav"
            subtitles = work / "narration.srt"
            assemble_timeline(report["scenes"], processed, duration, raw_timeline)
            print("Normalizing narration and mixing the original transition sounds", flush=True)
            report["narration_normalization"] = normalize_audio(raw_timeline, master, stereo_from_mono=True, target_peak=-1.8)
            has_source_audio = any(stream["codec_type"] == "audio" for stream in source_probe["streams"])
            if has_source_audio and args.sfx_gain > 0:
                ffmpeg("-loglevel", "error", "-i", str(master), "-i", str(args.source), "-filter_complex", f"[1:a:0]volume={args.sfx_gain}[sfx];[0:a:0][sfx]amix=inputs=2:duration=first:normalize=0,apad,atrim=duration={duration}[mixed]", "-map", "[mixed]", "-ar", str(RATE), "-ac", "2", "-c:a", "pcm_s24le", str(mixed))
            else:
                shutil.copyfile(master, mixed)
            report["mix_normalization"] = normalize_audio(mixed, mix_master, target_peak=-2.0)
            write_srt(cues, subtitles)
            original_subtitles = sum(stream["codec_type"] == "subtitle" for stream in source_probe["streams"])
            temporary_output = args.output.with_name(f".{args.output.stem}.{uuid.uuid4().hex}.mp4")
            print("Copying original video and subtitles; adding narrated audio and captions", flush=True)
            mux(args.source, mix_master, subtitles, temporary_output, original_subtitles)
            encoded_loudness = measure_loudness(temporary_output)
            if float(encoded_loudness["input_tp"]) > -1.5:
                attenuation = float(encoded_loudness["input_tp"]) + 1.75
                safer = work / "narration-mix-safer.wav"
                ffmpeg("-loglevel", "error", "-i", str(mix_master), "-af", f"volume=-{attenuation:.4f}dB", "-c:a", "pcm_s24le", str(safer))
                shutil.move(safer, mix_master)
                mux(args.source, mix_master, subtitles, temporary_output, original_subtitles)
                encoded_loudness = measure_loudness(temporary_output)
            if float(encoded_loudness["input_tp"]) > -1.5 or abs(float(encoded_loudness["input_i"]) + 16) > 1.0:
                raise ValueError(f"Encoded loudness is outside delivery limits: {encoded_loudness['input_i']} LUFS, {encoded_loudness['input_tp']} dBTP")
            print("Verifying unchanged video packets, original subtitles, duration, and chapters", flush=True)
            original_signature = packet_signature(args.source)
            narrated_signature = packet_signature(temporary_output)
            if original_signature != narrated_signature:
                raise ValueError("Video packets or timing changed; refusing to publish narrated output")
            output_probe = probe(temporary_output)
            if abs(float(output_probe["format"]["duration"]) - duration) > .05:
                raise ValueError("Narrated output duration changed")
            original_chapters = [(chapter["start_time"], chapter["end_time"], chapter.get("tags", {}).get("title")) for chapter in source_probe.get("chapters", [])]
            new_chapters = [(chapter["start_time"], chapter["end_time"], chapter.get("tags", {}).get("title")) for chapter in output_probe.get("chapters", [])]
            if original_chapters != new_chapters:
                raise ValueError("Original chapter markers changed")
            for index in range(original_subtitles):
                if packet_signature(args.source, f"s:{index}") != packet_signature(temporary_output, f"s:{index}"):
                    raise ValueError(f"Original subtitle stream {index} changed")
            if file_hash(args.source) != source_hash:
                raise ValueError("Original video changed during assembly; refusing to publish output")
            for row in report["scenes"]:
                if file_hash(args.voice_dir / f"{row['id']}.mp3") != row["source_audio_sha256"]:
                    raise ValueError(f"Voice file changed during assembly: {row['id']}; wait for generation to finish")
                if row["alignment"] and file_hash(Path(row["alignment"])) != row["alignment_sha256"]:
                    raise ValueError(f"Alignment changed during assembly: {row['id']}; wait for generation to finish")
            report.update({"verified_video_packets_identical": True, "video_packet_count": original_signature[1], "video_packet_signature": original_signature[0], "original_subtitle_streams_preserved": original_subtitles, "chapters_preserved": len(original_chapters), "narration_caption_cues": len(cues), "encoded_audio_lufs": float(encoded_loudness["input_i"]), "encoded_audio_true_peak_dbtp": float(encoded_loudness["input_tp"]), "output_duration": float(output_probe["format"]["duration"]), "output_sha256": file_hash(temporary_output), "dry_run": False})
            atomic_copy(master, args.voice_dir / "narration-master.wav")
            atomic_copy(mix_master, args.voice_dir / "narration-mix.wav")
            atomic_copy(subtitles, args.output.with_suffix(".srt"))
            atomic_json(args.voice_dir / "timing-report.json", report)
            os.replace(temporary_output, args.output)
            temporary_output = None
        print(f"Ready: {args.output}\nVideo packets unchanged. Audio: {report['encoded_audio_lufs']:.2f} LUFS / {report['encoded_audio_true_peak_dbtp']:.2f} dBTP.", flush=True)
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
        print(f"Narration assembly error: {error}", file=sys.stderr)
        return 1
    finally:
        if temporary_output is not None:
            temporary_output.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
