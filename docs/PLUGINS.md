# Writing a skill

A **skill** is a module of related capabilities. A **tool** is one callable the
language model may invoke. Adding either never requires modifying existing code.

## The shortest useful skill

```python
from typing import Annotated
from nova.skills import Param, Skill, tool


class WeatherSkill(Skill):
    name = "weather"
    description = "Look up the forecast."
    category = "Information"

    @tool("Get the current weather for a place.")
    async def current(
        self,
        location: Annotated[str, Param("City name", examples=("Leeds", "Berlin"))],
    ) -> str:
        # self.ctx gives you settings, the event bus and every other service.
        return f"It is 14 degrees and overcast in {location}."
```

Save it as `nova/skills/builtin/weather.py` and restart. It is discovered,
registered, and offered to the model — no registry edit, no import to add.

## How the schema is built

The JSON schema the model sees is derived from the method's type hints. There is
no second declaration to keep in sync, so the signature is always the truth.

| Python annotation | JSON Schema |
| --- | --- |
| `str`, `int`, `float`, `bool` | `string`, `integer`, `number`, `boolean` |
| `Literal["a", "b"]` | `string` with `enum` |
| `list[str]` | `array` of `string` |
| an `Enum` subclass | `string` with `enum` of values |
| a parameter with a default | omitted from `required` |

`Param(description=..., examples=...)` supplies the prose the model reads. Write
it for the model, not for a human: "Container name, e.g. jellyfin" beats
"the container".

## Risk levels

Every tool declares what it can do. This is what drives the confirmation flow.

```python
@tool("Restart a container.", destructive=True)
async def restart(self, name: Annotated[str, Param("Container name")]) -> str:
    ...
```

| Flag | Meaning | Behaviour |
| --- | --- | --- |
| *(none)* | Read-only | Runs immediately |
| `mutating=True` | Changes state, recoverable | Runs immediately |
| `destructive=True` | Data loss, reboot, service interruption | **Confirmed first** |

A destructive tool does not execute on its first call. The registry raises
`ConfirmationRequired` with a single-use, expiring token; the orchestrator turns
that into a spoken question, and the user's "yes" resolves it.

Mark a tool destructive if you would hesitate before running it yourself.

## Availability

Hide a skill when its backing service is not configured. Its tools are then
never shown to the model, which keeps the prompt small and stops the assistant
promising things it cannot do.

```python
def is_available(self) -> tuple[bool, str]:
    if not self.ctx.settings.weather.enabled:
        return False, "weather is disabled in settings"
    return True, ""
```

## Reaching other subsystems

```python
# None when the service is absent or degraded — check it.
system = self.ctx.service("system", SystemService)

# Or raise a user-legible error instead of an AttributeError.
system = self.ctx.require("system", SystemService)
```

Never import another skill. If two skills need the same logic, it belongs in a
module both can import.

## Adding context to the prompt

```python
def context_lines(self) -> list[str]:
    """Facts injected into the system prompt while this skill is available."""
    return [f"Weather is configured for {self.ctx.settings.assistant.location}."]
```

Use this sparingly — it runs on every turn and every line costs tokens.

`prompt_hint` is a class attribute for standing guidance, e.g. the memory skill
uses it to tell the model to store facts silently.

## Setup and teardown

```python
async def setup(self) -> None:
    """Prepare the skill. Raising here disables it."""
    self._client = httpx.AsyncClient(base_url=...)

async def teardown(self) -> None:
    await self._client.aclose()
```

## Distributing a skill

Three ways to ship one, in ascending order of trust required:

**1. Built-in** — a module in `nova/skills/builtin/`.

**2. A package** with an entry point:

```toml
[project.entry-points."nova.skills"]
weather = "nova_weather:WeatherSkill"
```

**3. A plugin file** — drop a `.py` into `~/.local/share/nova/plugins/`, or add
a directory to `plugins.search_paths`. Set `plugins.allow_third_party = false`
to disable this route entirely.

A skill can be switched off by name via `plugins.disabled`.

## Testing

```python
async def test_weather_reports_conditions(ctx):
    skill = WeatherSkill(ctx)
    await skill.setup()
    assert "degrees" in await skill.current("Leeds")
```

The `ctx` fixture in `tests/conftest.py` gives an isolated config and data
directory, so tests never touch a real profile.

Worth covering: the schema your tools produce, the failure path when the backing
service is down, and — if you marked anything destructive — that it does not run
before confirmation.

## Conventions

- Return strings the model can read aloud. Numbers get spoken, so
  `"CPU at 42 percent"` beats `{"cpu": 42.0}`.
- Raise `IntegrationError("service", "why")` for expected failures; the message
  reaches the user.
- Keep tools narrow. One tool that does one thing beats one tool with a `mode`
  parameter — the model picks correctly far more often.
- Do not log secrets. `self.log` output goes to disk.
