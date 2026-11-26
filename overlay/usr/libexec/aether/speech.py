#!/usr/bin/env python3
"""Offline speech input and output helpers for aetherctl."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

MODEL_PATH = "/usr/share/aether/models/vosk-model-small-en-us-0.15"
SAMPLE_RATE = 16000
MAX_RECORD_SECONDS = 30
MAX_FILE_SECONDS = 300
MAX_TEXT_LENGTH = 2000


def transcribe(path):
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("audio file must exist and be no larger than 10 MiB")
    try:
        audio = wave.open(str(source), "rb")
    except (wave.Error, OSError) as exc:
        raise ValueError("audio input must be a readable WAV file") from exc
    with audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != SAMPLE_RATE:
            raise ValueError("WAV input must be mono, 16-bit PCM at 16000 Hz")
        if audio.getnframes() > SAMPLE_RATE * MAX_FILE_SECONDS:
            raise ValueError("WAV input must be no longer than 5 minutes")
        try:
            from vosk import KaldiRecognizer, Model
        except ImportError as exc:
            raise RuntimeError("Vosk speech recognition is not installed") from exc

        model = Model(MODEL_PATH)
        recognizer = KaldiRecognizer(model, SAMPLE_RATE)
        while True:
            chunk = audio.readframes(4000)
            if not chunk:
                break
            recognizer.AcceptWaveform(chunk)
        return json.loads(recognizer.FinalResult()).get("text", "").strip()


def record(seconds):
    if not 1 <= seconds <= MAX_RECORD_SECONDS:
        raise ValueError("recording duration must be between 1 and 30 seconds")
    with tempfile.TemporaryDirectory(prefix="aether-speech-") as directory:
        recording = os.path.join(directory, "input.wav")
        try:
            subprocess.run(
                ["arecord", "-q", "-D", "default", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "wav", "-d", str(seconds), recording],
                check=True,
                capture_output=True,
                timeout=seconds + 5,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError("audio recording failed" + (": " + detail[:200] if detail else "")) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("audio recording timed out") from exc
        return transcribe(recording)


def speak(text):
    if not text or len(text) > MAX_TEXT_LENGTH:
        raise ValueError("speech text must contain between 1 and 2000 characters")
    try:
        audio = subprocess.run(["espeak-ng", "--stdout", text], check=True, capture_output=True, timeout=30).stdout
        subprocess.run(["aplay", "-q"], input=audio, check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("speech playback failed" + (": " + detail[:200] if detail else "")) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("speech playback timed out") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aetherspeech")
    actions = parser.add_subparsers(dest="action", required=True)
    listen = actions.add_parser("listen")
    listen.add_argument("--seconds", type=int, default=5)
    transcribe_parser = actions.add_parser("transcribe")
    transcribe_parser.add_argument("wav_path")
    say = actions.add_parser("say")
    say.add_argument("text", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.action == "listen":
            result = record(args.seconds)
            if result:
                print(result)
            return 0
        if args.action == "transcribe":
            print(transcribe(args.wav_path))
            return 0
        speak(" ".join(args.text))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
