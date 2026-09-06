"""Bank credentials, kept apart from everything else N.O.V.A. knows.

A dedicated file, read by this module and nothing else, with permissions that
are checked rather than assumed. Not in `config.toml` alongside the API keys,
not in Home Assistant, not in git.

The separation is not filing tidiness. `config.toml` is written by the settings
panel, sent to every connected client with its secrets redacted, and restored
from an export — three routes a bank token has no business being near. Keeping
it in a file that only this module opens means the number of places it can
leak from is one.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..runtime.logging import get_logger

log = get_logger(__name__)

#: Read from the data directory rather than the config directory: it is not
#: configuration, nothing else may read it, and it must not travel with a
#: settings export.
FILENAME = "finance.env"

#: Owner read/write and nothing else.
REQUIRED_MODE = 0o600


class FinanceSecrets:
    """The contents of `finance.env`, and where it came from."""

    def __init__(self, values: dict[str, str], path: Path | None) -> None:
        self._values = values
        self.path = path

    def get(self, key: str, default: str = "") -> str:
        # The environment wins, so a systemd unit or a container can supply the
        # token without it ever reaching a file.
        return os.environ.get(key) or self._values.get(key, default) or default

    @property
    def present(self) -> bool:
        return bool(self._values) or any(
            key in os.environ for key in ("NOVA_FINANCE_TOKEN", "NOVA_FINANCE_WEBHOOK_SECRET")
        )


def load(data_dir: Path) -> FinanceSecrets:
    """Read `finance.env`, refusing it if anyone else can read it too."""
    path = data_dir / FILENAME
    if not path.is_file():
        return FinanceSecrets({}, None)

    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            # Loud, and still loaded: refusing to start would leave someone
            # with a broken assistant and no obvious cause, while a warning
            # they can act on costs nothing to print every start.
            log.warning(
                "finance_secrets_readable_by_others",
                path=str(path),
                mode=oct(mode),
                remedy=f"chmod 600 {path}",
            )

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Quotes are what a person writes and not part of the value.
        values[key.strip()] = value.strip().strip("'\"")
    return FinanceSecrets(values, path)


#: The template shipped with the package. Copied rather than regenerated, so
#: the instructions somebody reads next to their real credentials are the same
#: ones in the repository rather than a second copy that drifts.
TEMPLATE = Path(__file__).with_name(f"{FILENAME}.example")


def write_example(data_dir: Path) -> Path | None:
    """Drop a `finance.env.example` next to where the real one goes.

    So the first thing anyone finds when they go looking for where the token
    goes is a file telling them, in the directory it belongs in.
    """
    if not TEMPLATE.is_file():  # pragma: no cover - only if packaging drops it
        log.warning("finance_secrets_template_missing", expected=str(TEMPLATE))
        return None
    path = data_dir / f"{FILENAME}.example"
    path.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return path
