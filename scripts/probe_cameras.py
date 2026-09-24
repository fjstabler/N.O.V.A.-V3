#!/usr/bin/env python3
"""Find which camera index actually gives N.O.V.A. a live picture.

The single most common reason every camera feature "does nothing" is that the
configured index points at the wrong /dev/video node — a webcam often exposes
several, and only one carries the real picture; the others open fine but hand
back a black frame. This probes indices 0-9 and, for each, reports the
resolution, how bright the picture is, and whether it is actually changing, so
you can set the right one in **Vision → Camera index**.

    python scripts/probe_cameras.py

No N.O.V.A. instance needs to be running. It only needs the vision extra
(OpenCV), which the camera features need anyway:

    .venv/bin/pip install -e "services/core[vision]"
"""

from __future__ import annotations

import sys
import time

#: Below this average brightness (0-255) a frame is treated as black — the
#: camera opened but isn't really capturing.
BLANK_THRESHOLD = 8.0
#: Average per-pixel change between two frames above which the picture is "live".
LIVE_THRESHOLD = 1.0
MAX_INDEX = 10


def main() -> int:
    try:
        import cv2
    except ImportError:
        print("OpenCV isn't installed. Run:  .venv/bin/pip install -e 'services/core[vision]'")
        return 1
    import numpy as np

    print(f"Probing camera indices 0-{MAX_INDEX - 1}…\n")
    usable: list[int] = []

    for index in range(MAX_INDEX):
        capture = cv2.VideoCapture(index)
        if not capture.isOpened():
            capture.release()
            continue

        # The first frames off a cold sensor are usually black; discard a few.
        for _ in range(5):
            capture.read()
        ok_first, first = capture.read()
        time.sleep(0.3)
        ok_second, second = capture.read()
        capture.release()

        if not ok_first or first is None:
            print(f"  index {index}: opens, but returns no image")
            continue

        height, width = first.shape[:2]
        brightness = float(first.mean())
        changing = (
            ok_second
            and second is not None
            and float(np.abs(first.astype(int) - second.astype(int)).mean()) > LIVE_THRESHOLD
        )

        if brightness < BLANK_THRESHOLD:
            verdict, mark = "black (not really capturing)", "✗  skip"
        elif changing:
            verdict, mark = "live", "✓  USABLE"
            usable.append(index)
        else:
            verdict, mark = "static (a still picture — maybe usable)", "•  maybe"
            usable.append(index)

        print(
            f"  index {index}: {width}x{height}, bright {brightness:3.0f}/255, {verdict}  {mark}"
        )

    print()
    if usable:
        best = usable[0]
        print(f"Usable camera index(es): {usable}.")
        print(
            f"Set **Vision → Camera index** to {best} "
            "(if that shows the wrong view, try the next one)."
        )
        print("Then, for 'show me the bedroom', add it under Vision → Named cameras: ")
        print(f"    name: bedroom   index: {best}")
    else:
        print("No usable camera found. Check that it is:")
        print("  • plugged in and not covered,")
        print("  • not already in use by another app (Zoom, browser tab, Cheese…),")
        print("  • accessible — on Linux add yourself to the 'video' group:")
        print("        sudo usermod -aG video $USER   # then log out and back in")
    return 0


if __name__ == "__main__":
    sys.exit(main())
