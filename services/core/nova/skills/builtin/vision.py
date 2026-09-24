"""Vision: screen and camera understanding.

Capture is local; interpretation is the one place besides reasoning where an
image leaves the machine, so it is opt-in (``vision.enabled``) and images are
downscaled and JPEG-compressed before they go anywhere — smaller uploads, lower
cost, and no more resolution than the question needs.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Annotated

from ...ai.client import OpenAIClient
from ...ai.orchestrator import Orchestrator
from ...integrations.local_camera import capture_camera_rgb, encode_jpeg
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
        raw, width, height = capture_camera_rgb(index)
        return self._encode(raw, width, height, "RGB")

    def _encode(self, raw: bytes, width: int, height: int, mode: str) -> bytes:
        settings = self.ctx.settings.vision
        return encode_jpeg(
            raw,
            width,
            height,
            mode=mode,
            max_edge=settings.max_image_edge,
            quality=settings.jpeg_quality,
        )

    async def _describe(self, question: str, image: bytes) -> str:
        encoded = base64.b64encode(image).decode("ascii")
        data_url = f"data:image/jpeg;base64,{encoded}"
        self.log.info("vision_request", bytes=len(image), question=question[:80])
        return await self._client().describe_image(
            question, data_url, model=self.ctx.settings.openai.vision_model
        )
