"""On-device camera detection: motion and faces, without the cloud.

Where the vision skill *describes* a camera by sending a frame to a cloud model,
this *detects* on the machine itself — instantly, privately, with no key needed:

* **motion** — two frames a moment apart, differenced in NumPy: "is anything
  moving in here?"
* **faces / people** — the same local YuNet detector room-watch uses, to count
  who is in view and name anyone enrolled.

Both degrade cleanly: motion needs only NumPy, and the face count borrows the
security service's engine, reporting plainly if OpenCV or the model files are
not installed rather than failing.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from ...integrations.detection import MotionDetector
from ...integrations.local_camera import local_camera_pool
from ...runtime.errors import MissingDependency, MissingModel, SkillError
from ..base import Param, Skill, tool


class CameraSkill(Skill):
    name = "camera"
    description = "Detect motion and count people on a local camera, entirely on-device."
    category = "Vision"

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.vision.camera_enabled:
            return False, "the camera is disabled in settings"
        return True, ""

    def _camera_index(self, requested: int) -> int:
        return requested if requested >= 0 else self.ctx.settings.vision.camera_index

    @tool(
        "Check a camera for movement right now — two quick frames compared "
        "on-device. Use for 'is anything moving', 'is the room still', 'did "
        "something just move in the kitchen'. No image leaves the machine."
    )
    async def check_for_motion(
        self,
        camera_index: Annotated[int, Param("Which camera; -1 uses the default")] = -1,
        seconds: Annotated[float, Param("Gap between the two frames, in seconds")] = 1.0,
    ) -> str:
        index = self._camera_index(camera_index)
        seconds = max(0.2, min(seconds, 5.0))
        first = await local_camera_pool.read_bgr(index)
        await asyncio.sleep(seconds)
        second = await local_camera_pool.read_bgr(index)
        detector = MotionDetector(min_area_fraction=self.ctx.settings.vision.motion_sensitivity)
        return await asyncio.to_thread(lambda: detector.detect(first, second).describe())

    @tool(
        "Count the people visible on a camera and name any it recognises — the "
        "same on-device face detection room-watch uses. Use for 'is anyone in "
        "the office', 'how many people are here', 'who's in the room'."
    )
    async def count_people(
        self,
        camera_index: Annotated[int, Param("Which camera; -1 uses the default")] = -1,
    ) -> str:
        security = self.ctx.service("security")
        if security is None:
            raise SkillError("on-device face detection needs the security service running")
        engine, faces = security.engine, security.faces
        if not engine.loaded:
            try:
                await asyncio.to_thread(engine.load)
            except (MissingDependency, MissingModel) as exc:
                raise SkillError(str(exc)) from exc

        index = self._camera_index(camera_index)
        frame = await local_camera_pool.read_bgr(index)
        observations = await asyncio.to_thread(engine.observe, frame)
        if not observations:
            return "I can't see anyone on the camera."

        threshold = self.ctx.settings.security.match_threshold
        known: list[str] = []
        for obs in observations:
            match = faces.match(obs.embedding, threshold=threshold)
            if match is not None:
                known.append(match[0])

        count = len(observations)
        people = "person" if count == 1 else "people"
        if known:
            names = ", ".join(sorted(set(known)))
            recognised = f" I recognise {names}."
        else:
            recognised = "" if not faces.names() else " I don't recognise anyone."
        return f"I can see {count} {people}.{recognised}"
