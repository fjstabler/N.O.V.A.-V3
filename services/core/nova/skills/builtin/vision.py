"""Vision: screen and camera understanding.

Capture is local; interpretation is the one place besides reasoning where an
image leaves the machine, so it is opt-in (``vision.enabled``) and images are
downscaled and JPEG-compressed before they go anywhere — smaller uploads, lower
cost, and no more resolution than the question needs.
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Annotated

from ...ai.client import OpenAIClient
from ...ai.orchestrator import Orchestrator
from ...runtime.errors import MissingDependency, SkillError
from ..base import Param, Skill, tool


class VisionSkill(Skill):
    name = "vision"
    description = "Look at the screen or a camera and describe what is there."
    category = "Vision"

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.vision.enabled:
            return False, "vision disabled in settings"
        if not self.ctx.settings.openai.configured:
            return False, "image understanding needs an OpenAI key"
        return True, ""

    def _client(self) -> OpenAIClient:
        orchestrator = self.ctx.service("orchestrator", Orchestrator)
        if orchestrator is None:
            raise SkillError("reasoning is not available")
        return orchestrator.client

    @tool("Look at the screen and answer a question about what is displayed.")
    async def look_at_screen(
        self,
        question: Annotated[
            str, Param("What to look for", examples=("what error is shown?", "describe this"))
        ] = "Describe what is on the screen, concisely.",
        monitor: Annotated[int, Param("Monitor number; 0 means all of them")] = 1,
    ) -> str:
        if not self.ctx.settings.vision.screen_capture:
            raise SkillError("screen capture is disabled in settings")
        image = await asyncio.to_thread(self._capture_screen, monitor)
        return await self._describe(question, image)

    @tool("Look through a camera and answer a question about what it sees.")
    async def look_at_camera(
        self,
        question: Annotated[str, Param("What to look for")] = "Describe what the camera sees.",
        camera_index: Annotated[int, Param("Which camera; -1 uses the configured default")] = -1,
    ) -> str:
        if not self.ctx.settings.vision.camera_enabled:
            raise SkillError("the camera is disabled in settings")
        index = camera_index if camera_index >= 0 else self.ctx.settings.vision.camera_index
        image = await asyncio.to_thread(self._capture_camera, index)
        return await self._describe(question, image)

    # ----------------------------------------------------------------- capture

    def _capture_screen(self, monitor: int) -> bytes:
        try:
            import mss
        except ImportError as exc:
            raise MissingDependency("screen capture", "mss", "vision") from exc

        with mss.mss() as screen:
            monitors = screen.monitors
            index = 0 if monitor == 0 else max(1, min(monitor, len(monitors) - 1))
            shot = screen.grab(monitors[index])
            return self._encode(shot.rgb, shot.size.width, shot.size.height, "RGB")

    def _capture_camera(self, index: int) -> bytes:
        try:
            import cv2
        except ImportError as exc:
            raise MissingDependency("camera", "opencv-python-headless", "vision") from exc

        capture = cv2.VideoCapture(index)
        try:
            if not capture.isOpened():
                raise SkillError(f"camera {index} could not be opened")
            # The first frame off a cold sensor is usually black; discard a few.
            for _ in range(5):
                capture.read()
            ok, frame = capture.read()
            if not ok or frame is None:
                raise SkillError("the camera did not return an image")
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width = rgb.shape[:2]
            return self._encode(rgb.tobytes(), width, height, "RGB")
        finally:
            capture.release()

    def _encode(self, raw: bytes, width: int, height: int, mode: str) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:
            raise MissingDependency("image encoding", "pillow", "vision") from exc

        settings = self.ctx.settings.vision
        image = Image.frombytes(mode, (width, height), raw)
        longest = max(image.size)
        if longest > settings.max_image_edge:
            scale = settings.max_image_edge / longest
            image = image.resize(
                (int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS
            )
        buffer = io.BytesIO()
        image.convert("RGB").save(
            buffer, format="JPEG", quality=settings.jpeg_quality, optimize=True
        )
        return buffer.getvalue()

    async def _describe(self, question: str, image: bytes) -> str:
        encoded = base64.b64encode(image).decode("ascii")
        data_url = f"data:image/jpeg;base64,{encoded}"
        self.log.info("vision_request", bytes=len(image), question=question[:80])
        return await self._client().describe_image(
            question, data_url, model=self.ctx.settings.openai.vision_model
        )
