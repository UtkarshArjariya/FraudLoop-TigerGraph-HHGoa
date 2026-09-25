#!/usr/bin/env python3
"""Generate and cache ElevenLabs narration without storing credentials.

Only narration text goes to the official ElevenLabs API. The API key comes from
ELEVENLABS_API_KEY or an explicitly supplied, private temporary file. Video,
images, datasets, configuration and saved case payloads are never uploaded.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
VOICE_ID = "onwK4e9ZLuTAKqWW03F9"
VOICE_NAME = "Daniel - Steady Broadcaster"
MODEL = "eleven_multilingual_v2"


def atomic_write(path: Path, content: bytes):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "narration.json")
    parser.add_argument("--output", type=Path, default=ROOT / "out/voiceover")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--voice-id", default=VOICE_ID)
    parser.add_argument("--speed", type=float, default=1.03)
    parser.add_argument("--scene", action="append", help="Generate selected scene IDs only")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    scenes = json.loads(args.manifest.read_text())["scenes"]
    args.output.mkdir(parents=True, exist_ok=True)
    settings = {"stability": 0.55, "similarity_boost": 0.75, "style": 0.1,
                "use_speaker_boost": True, "speed": args.speed}
    pending = []
    for index, scene in enumerate(scenes):
        if args.scene and scene["id"] not in args.scene:
            continue
        body = {"text": scene["text"], "model_id": MODEL,
                "voice_settings": settings, "seed": 731 + index,
                "previous_text": scenes[index - 1]["text"] if index else "",
                "next_text": scenes[index + 1]["text"] if index + 1 < len(scenes) else ""}
        digest = hashlib.sha256(json.dumps({"voice": args.voice_id, "body": body}, sort_keys=True).encode()).hexdigest()
        audio = args.output / f"{scene['id']}.mp3"
        metadata = args.output / f"{scene['id']}.alignment.json"
        if audio.exists() and metadata.exists():
            cached = json.loads(metadata.read_text())
            if cached.get("request_fingerprint") == digest:
                print(f"{scene['id']}: cached", flush=True)
                continue
        pending.append((scene, body, digest, audio, metadata))
    count = sum(len(scene["text"]) for scene, *_ in pending)
    print(f"Pending: {len(pending)} scenes, {count} narration characters; {MODEL}", flush=True)
    if args.dry_run or not pending:
        return
    key = (args.key_file.read_text().strip() if args.key_file else os.getenv("ELEVENLABS_API_KEY", "").strip())
    if not key:
        raise SystemExit("Set ELEVENLABS_API_KEY privately or use --key-file. No key is stored by this script.")
    for scene, body, digest, audio, metadata in pending:
        request = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{args.voice_id}/with-timestamps?output_format=mp3_44100_128",
            data=json.dumps(body).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        result = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    result = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read()).get("detail", {})
                    status = detail.get("status", "api_error") if isinstance(detail, dict) else "api_error"
                except (ValueError, AttributeError):
                    status = "api_error"
                # Print only a stable status code; never remote messages or headers.
                if exc.code in (502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise SystemExit(f"{scene['id']}: ElevenLabs HTTP {exc.code}, status={status}. Cached scenes preserved.") from None
            except (urllib.error.URLError, TimeoutError):
                # A transport failure may occur after billing. Stop, never blindly duplicate a request.
                raise SystemExit(f"{scene['id']}: connection failed; stopped without automatic regeneration.") from None
        if not isinstance(result, dict) or not result.get("audio_base64"):
            raise SystemExit(f"{scene['id']}: no audio returned; generation stopped.")
        raw = base64.b64decode(result["audio_base64"], validate=True)
        if len(raw) < 100:
            raise SystemExit(f"{scene['id']}: returned audio was empty.")
        record = {"provider": "ElevenLabs", "voice_id": args.voice_id,
                  "voice_name": VOICE_NAME if args.voice_id == VOICE_ID else "User-selected voice",
                  "model_id": MODEL, "voice_settings": settings, "scene_id": scene["id"],
                  "text": scene["text"], "request_fingerprint": digest,
                  "alignment": result.get("alignment"),
                  "normalized_alignment": result.get("normalized_alignment")}
        atomic_write(audio, raw)
        atomic_write(metadata, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
        ends = (record.get("alignment") or {}).get("character_end_times_seconds", [])
        print(f"{scene['id']}: generated {len(raw)} bytes; aligned speech end {max(ends, default=0):.2f}s", flush=True)


if __name__ == "__main__":
    main()
