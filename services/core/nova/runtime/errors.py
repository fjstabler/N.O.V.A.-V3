"""Error taxonomy for the N.O.V.A. core.

Every failure the user can plausibly hit gets a distinct type so the UI can
decide whether to surface it, retry it, or silently degrade. Anything that
inherits from :class:`DegradedCapability` is *expected* on a machine that has
not installed an optional extra — it must never take the process down.
"""

from __future__ import annotations


class NovaError(Exception):
    """Base class for all N.O.V.A. errors."""

    #: Stable machine-readable code sent to the UI.
    code = "nova.error"
    #: Whether the UI should show this to the user as a notification.
    user_visible = True

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_payload(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ConfigError(NovaError):
    code = "nova.config"


class ServiceStartupError(NovaError):
    code = "nova.service.startup"


class DegradedCapability(NovaError):
    """A subsystem cannot run, but the assistant remains usable without it.

    Raised during service start when an optional dependency or model file is
    missing. The supervisor records it and keeps going.
    """

    code = "nova.capability.unavailable"
    user_visible = False

    def __init__(self, capability: str, reason: str, *, remedy: str | None = None) -> None:
        super().__init__(f"{capability} unavailable: {reason}", detail=remedy)
        self.capability = capability
        self.reason = reason
        self.remedy = remedy

    def as_payload(self) -> dict[str, object]:
        return {
            "code": self.code,
            "capability": self.capability,
            "message": self.message,
            "remedy": self.remedy,
        }


class MissingDependency(DegradedCapability):
    """An optional Python package is not installed."""

    code = "nova.capability.missing_dependency"

    def __init__(self, capability: str, package: str, extra: str) -> None:
        super().__init__(
            capability,
            f"python package '{package}' is not installed",
            remedy=f"pip install 'nova-core[{extra}]'",
        )
        self.package = package
        self.extra = extra


class MissingModel(DegradedCapability):
    """A local model file has not been downloaded yet."""

    code = "nova.capability.missing_model"

    def __init__(self, capability: str, path: str) -> None:
        super().__init__(
            capability,
            f"model file not found at {path}",
            remedy="run: python scripts/fetch_models.py",
        )
        self.path = path


class SkillError(NovaError):
    code = "nova.skill"


class ToolExecutionError(SkillError):
    code = "nova.skill.tool"

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"tool '{tool}' failed: {message}")
        self.tool = tool


class PermissionDenied(NovaError):
    """A privileged action was refused by the policy engine."""

    code = "nova.permission_denied"


class ConfirmationRequired(NovaError):
    """A destructive action needs explicit user confirmation before running."""

    code = "nova.confirmation_required"
    user_visible = False

    def __init__(self, action: str, summary: str, token: str) -> None:
        super().__init__(f"confirmation required for {action}")
        self.action = action
        self.summary = summary
        self.token = token

    def as_payload(self) -> dict[str, object]:
        return {
            "code": self.code,
            "action": self.action,
            "summary": self.summary,
            "token": self.token,
        }


class FinalAnswer(NovaError):
    """A tool's own words, to be delivered unchanged.

    Raised by a tool whose result must not go back to the model. Ordinary tool
    results are appended to the message list and sent with the next request, so
    a tool that returns figures is a tool that puts those figures in a prompt —
    which is exactly what the finance module is forbidden from doing with bank
    data.

    It also fixes the phrasing. A model handed "£340 available, 11 days to
    payday" will helpfully conclude "so yes, you can afford it", and a verdict
    from a system its owner can reprogram is not a constraint — it is an
    invitation to argue with the tool instead of reading the number.

    Not an error despite the base class: raising is simply the only way a tool
    can end a turn, since returning hands the value to the model.
    """

    code = "nova.final_answer"

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class IntegrationError(NovaError):
    code = "nova.integration"

    def __init__(self, integration: str, message: str) -> None:
        super().__init__(f"{integration}: {message}")
        self.integration = integration
