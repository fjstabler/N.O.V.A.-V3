"""Tool schema derivation, dispatch and the confirmation gate."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest

from nova.context import NovaContext
from nova.runtime.errors import ConfirmationRequired, ToolExecutionError
from nova.skills.base import Param, Skill, build_parameter_schema, tool
from nova.skills.registry import SkillRegistry


class SampleSkill(Skill):
    name = "sample"
    description = "A skill used by the tests."

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.calls: list[tuple] = []

    @tool("Echo a message back.")
    async def echo(
        self,
        message: Annotated[str, Param("What to say back")],
        times: Annotated[int, Param("How many repeats")] = 1,
    ) -> str:
        self.calls.append(("echo", message, times))
        return " ".join([message] * times)

    @tool("Set a mode.", mutating=True)
    async def set_mode(self, mode: Annotated[Literal["fast", "slow"], Param("Which mode")]) -> str:
        self.calls.append(("set_mode", mode))
        return f"mode={mode}"

    @tool("Delete everything.", destructive=True)
    async def wipe(self, target: Annotated[str, Param("What to wipe")]) -> str:
        self.calls.append(("wipe", target))
        return f"wiped {target}"

    @tool("Raise an error.")
    async def explode(self) -> str:
        raise ValueError("intentional")

    async def not_a_tool(self) -> str:
        return "invisible"


# --------------------------------------------------------------- schema build


def test_schema_is_derived_from_the_signature() -> None:
    schema = build_parameter_schema(SampleSkill.echo)
    assert schema["type"] == "object"
    assert schema["properties"]["message"]["type"] == "string"
    assert schema["properties"]["message"]["description"] == "What to say back"
    assert schema["properties"]["times"]["type"] == "integer"
    assert schema["required"] == ["message"]  # `times` has a default
    assert schema["additionalProperties"] is False


def test_literals_become_enums() -> None:
    schema = build_parameter_schema(SampleSkill.set_mode)
    assert schema["properties"]["mode"]["enum"] == ["fast", "slow"]


def test_self_is_never_a_parameter() -> None:
    assert "self" not in build_parameter_schema(SampleSkill.echo)["properties"]


def test_only_decorated_methods_are_exposed(ctx: NovaContext) -> None:
    names = {spec.name for spec in SampleSkill(ctx).collect_tools()}
    assert names == {"echo", "set_mode", "wipe", "explode"}
    assert "not_a_tool" not in names


def test_tools_are_namespaced_by_skill(ctx: NovaContext) -> None:
    spec = next(s for s in SampleSkill(ctx).collect_tools() if s.name == "echo")
    assert spec.qualified_name == "sample_echo"
    assert spec.as_openai_tool()["function"]["name"] == "sample_echo"


def test_destructive_implies_mutating(ctx: NovaContext) -> None:
    spec = next(s for s in SampleSkill(ctx).collect_tools() if s.name == "wipe")
    assert spec.destructive and spec.mutating


def test_a_non_async_tool_is_rejected_at_definition_time() -> None:
    with pytest.raises(TypeError, match="must be an async function"):

        class Broken(Skill):
            @tool("Not async.")
            def sync_tool(self) -> str:  # type: ignore[misc]
                return "no"


# -------------------------------------------------------------------- registry


async def registry_with_sample(ctx: NovaContext) -> tuple[SkillRegistry, SampleSkill]:
    registry = SkillRegistry(ctx)
    skill = SampleSkill(ctx)
    await skill.setup()
    registry._skills[skill.name] = skill
    for spec in skill.collect_tools():
        registry._tools[spec.qualified_name] = spec
    return registry, skill


async def test_dispatch_calls_the_bound_method(ctx: NovaContext) -> None:
    registry, skill = await registry_with_sample(ctx)
    result = await registry.call("sample_echo", {"message": "hi", "times": 2})
    assert result == "hi hi"
    assert skill.calls == [("echo", "hi", 2)]


async def test_arguments_accept_the_models_json_string(ctx: NovaContext) -> None:
    registry, _ = await registry_with_sample(ctx)
    assert await registry.call("sample_echo", '{"message": "json"}') == "json"


async def test_malformed_json_arguments_are_reported(ctx: NovaContext) -> None:
    registry, _ = await registry_with_sample(ctx)
    with pytest.raises(ToolExecutionError, match="valid JSON"):
        await registry.call("sample_echo", "{not json")


async def test_hallucinated_arguments_are_dropped(ctx: NovaContext) -> None:
    """Models invent fields; that should not fail an otherwise valid call."""
    registry, _ = await registry_with_sample(ctx)
    result = await registry.call("sample_echo", {"message": "ok", "nonexistent": "ignore me"})
    assert result == "ok"


async def test_missing_required_arguments_are_reported(ctx: NovaContext) -> None:
    registry, _ = await registry_with_sample(ctx)
    with pytest.raises(ToolExecutionError, match="missing required"):
        await registry.call("sample_echo", {})


async def test_unknown_tools_are_rejected(ctx: NovaContext) -> None:
    registry, _ = await registry_with_sample(ctx)
    with pytest.raises(ToolExecutionError, match="unknown tool"):
        await registry.call("sample_nonexistent", {})


async def test_exceptions_become_tool_errors_not_crashes(ctx: NovaContext) -> None:
    registry, _ = await registry_with_sample(ctx)
    with pytest.raises(ToolExecutionError, match="intentional"):
        await registry.call("sample_explode", {})


# ---------------------------------------------------------------- confirmation


async def test_destructive_tools_do_not_run_on_first_call(ctx: NovaContext) -> None:
    registry, skill = await registry_with_sample(ctx)
    with pytest.raises(ConfirmationRequired) as excinfo:
        await registry.call("sample_wipe", {"target": "everything"})

    assert excinfo.value.token
    assert "everything" in excinfo.value.summary
    assert skill.calls == []  # crucially, nothing happened


async def test_confirming_runs_the_stashed_call(ctx: NovaContext) -> None:
    registry, skill = await registry_with_sample(ctx)
    try:
        await registry.call("sample_wipe", {"target": "logs"})
    except ConfirmationRequired as request:
        token = request.token

    assert await registry.confirm(token) == "wiped logs"
    assert skill.calls == [("wipe", "logs")]


async def test_a_confirmation_token_is_single_use(ctx: NovaContext) -> None:
    registry, _ = await registry_with_sample(ctx)
    try:
        await registry.call("sample_wipe", {"target": "logs"})
    except ConfirmationRequired as request:
        token = request.token

    await registry.confirm(token)
    with pytest.raises(Exception, match="expired"):
        await registry.confirm(token)


async def test_cancelling_discards_the_pending_action(ctx: NovaContext) -> None:
    registry, skill = await registry_with_sample(ctx)
    try:
        await registry.call("sample_wipe", {"target": "logs"})
    except ConfirmationRequired as request:
        token = request.token

    assert registry.cancel(token) is True
    with pytest.raises(Exception, match="expired"):
        await registry.confirm(token)
    assert skill.calls == []


async def test_expired_confirmations_are_refused(ctx: NovaContext) -> None:
    """A 'yes' ten minutes later must not reboot the server."""
    import nova.skills.registry as registry_module

    original = registry_module.CONFIRMATION_TTL
    registry_module.CONFIRMATION_TTL = -1.0
    try:
        registry, skill = await registry_with_sample(ctx)
        try:
            await registry.call("sample_wipe", {"target": "logs"})
        except ConfirmationRequired as request:
            token = request.token
        with pytest.raises(Exception, match="expired"):
            await registry.confirm(token)
        assert skill.calls == []
    finally:
        registry_module.CONFIRMATION_TTL = original


async def test_latest_pending_lets_a_bare_yes_resolve(ctx: NovaContext) -> None:
    registry, _ = await registry_with_sample(ctx)
    assert registry.latest_pending() is None
    with pytest.raises(ConfirmationRequired):
        await registry.call("sample_wipe", {"target": "cache"})

    pending = registry.latest_pending()
    assert pending is not None
    assert "cache" in pending[1].summary


async def test_non_destructive_mutations_run_immediately(ctx: NovaContext) -> None:
    registry, skill = await registry_with_sample(ctx)
    assert await registry.call("sample_set_mode", {"mode": "fast"}) == "mode=fast"
    assert skill.calls == [("set_mode", "fast")]
