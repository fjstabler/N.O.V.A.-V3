#!/usr/bin/env python3
"""Download the local models the voice stack needs.

Everything here is open weights and free. Nothing is bundled in the repository
because the files are large binaries and the licences want them fetched from
source, so this script is the one-time step between a fresh clone and a working
microphone.

Downloads are resumable, checksummed where an official digest exists, and
written atomically — a half-downloaded ONNX file that looks complete is worse
than no file at all.

    python scripts/fetch_models.py            # everything
    python scripts/fetch_models.py --only tts # just Kokoro
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"


@dataclass(frozen=True)
class Artifact:
    group: str
    name: str
    url: str
    destination: str
    approximate_mb: int
    sha256: str = ""


ARTIFACTS: tuple[Artifact, ...] = (
    Artifact("tts", "Kokoro voice model", f"{KOKORO_BASE}/kokoro-v1.0.onnx", "kokoro/kokoro-v1.0.onnx", 310),
    Artifact("tts", "Kokoro voice pack", f"{KOKORO_BASE}/voices-v1.0.bin", "kokoro/voices-v1.0.bin", 27),
)


def models_dir() -> Path:
    """Mirror nova.config.paths so both agree on where models live."""
    override = os.environ.get("NOVA_HOME")
    if override:
        return Path(override).expanduser().resolve() / "models"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "NOVA"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "NOVA"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "nova"
    return base / "models"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def download(artifact: Artifact, target: Path, *, force: bool) -> bool:
    if target.exists() and not force:
        print(f"  ✓ {artifact.name} already present ({human(target.stat().st_size)})")
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temporary file so an interrupted download is never mistaken
    # for a complete model on the next run.
    partial = target.with_suffix(target.suffix + ".part")
    print(f"  ↓ {artifact.name} (~{artifact.approximate_mb} MB)")

    try:
        request = urllib.request.Request(artifact.url, headers={"User-Agent": "nova-fetch-models"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            digest = hashlib.sha256()
            written = 0
            while chunk := response.read(1 << 20):
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if total:
                    percent = written / total * 100
                    print(f"\r    {percent:5.1f}%  {human(written)}", end="", flush=True)
            print()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        partial.unlink(missing_ok=True)
        print(f"    failed: {exc}", file=sys.stderr)
        return False

    if artifact.sha256 and digest.hexdigest() != artifact.sha256:
        partial.unlink(missing_ok=True)
        print(f"    checksum mismatch for {artifact.name}", file=sys.stderr)
        return False

    partial.replace(target)
    return True


def check_whisper() -> None:
    """faster-whisper downloads its own weights on first use."""
    print("\nSpeech recognition (faster-whisper)")
    try:
        import faster_whisper  # noqa: F401

        print("  ✓ installed — model weights download automatically on first run")
    except ImportError:
        print("  · not installed — run: pip install 'nova-core[voice]'")


def check_openwakeword() -> None:
    """openWakeWord ships a downloader for its bundled models."""
    print("\nWake word (openWakeWord)")
    try:
        import openwakeword.utils
    except ImportError:
        print("  · not installed — run: pip install 'nova-core[voice]'")
        return
    try:
        openwakeword.utils.download_models()
        print("  ✓ bundled wake word models are present")
    except Exception as exc:  # noqa: BLE001 - network or permissions
        print(f"  ! could not fetch bundled models: {exc}", file=sys.stderr)

    print(
        "\n  Note: 'hey nova' is not a bundled phrase. Either train one at\n"
        "  https://github.com/dscripka/openWakeWord and point\n"
        "  voice.wake.model at the .onnx file, or set it to a bundled\n"
        "  phrase such as 'hey_jarvis' or 'alexa'."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Download N.O.V.A.'s local models")
    parser.add_argument("--only", choices=sorted({a.group for a in ARTIFACTS}), help="fetch one group")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()

    target_dir = models_dir()
    print(f"Model directory: {target_dir}\n")

    free = shutil.disk_usage(target_dir.parent if target_dir.parent.exists() else Path.home()).free
    needed = sum(a.approximate_mb for a in ARTIFACTS if not args.only or a.group == args.only)
    if free < needed * 1024 * 1024 * 1.2:
        print(f"Not enough free space: need ~{needed} MB, have {human(free)}", file=sys.stderr)
        return 1

    print("Speech synthesis (Kokoro)")
    failures = 0
    for artifact in ARTIFACTS:
        if args.only and artifact.group != args.only:
            continue
        if not download(artifact, target_dir / artifact.destination, force=args.force):
            failures += 1

    if not args.only:
        check_whisper()
        check_openwakeword()

    if failures:
        print(f"\n{failures} download(s) failed.", file=sys.stderr)
        return 1

    print("\nDone. Start the core with: python -m nova")
    return 0


if __name__ == "__main__":
    sys.exit(main())
