"""Command execution with a policy gate.

Letting a language model run shell commands on a server is the single most
dangerous thing this project does, so the rule is: *classify first, execute
second*, and never execute anything the classifier does not recognise.

Four risk levels:

``READ_ONLY``    inspection only — runs immediately.
``MUTATING``     changes state but is recoverable — runs immediately.
``DESTRUCTIVE``  data loss, reboots, service interruption — needs confirmation.
``FORBIDDEN``    never runs, regardless of confirmation.

Commands run without a shell by default (``shell=False``), so quoting bugs
cannot turn one command into two. Pipelines and redirection require the operator
to have opted in via ``server.allow_shell``, and are never classified below
``MUTATING``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from ..runtime.errors import PermissionDenied
from ..runtime.logging import get_logger

log = get_logger(__name__)


class Risk(IntEnum):
    READ_ONLY = 0
    MUTATING = 1
    DESTRUCTIVE = 2
    FORBIDDEN = 3


#: Binaries that only observe. Anything not listed is never read-only.
READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "find",
        "locate",
        "df",
        "du",
        "free",
        "uptime",
        "uname",
        "hostname",
        "hostnamectl",
        "whoami",
        "id",
        "date",
        "cal",
        "wc",
        "stat",
        "file",
        "tree",
        "pwd",
        "env",
        "printenv",
        "which",
        "type",
        "ps",
        "pgrep",
        "pidof",
        "lsof",
        "top",
        "htop",
        "vmstat",
        "iostat",
        "mpstat",
        "sensors",
        "nvidia-smi",
        "lscpu",
        "lsblk",
        "lspci",
        "lsusb",
        "blkid",
        "dmidecode",
        "ip",
        "ss",
        "netstat",
        "ping",
        "traceroute",
        "dig",
        "nslookup",
        "host",
        "arp",
        "journalctl",
        "dmesg",
        "last",
        "w",
        "who",
        "docker",
        "podman",
        "kubectl",
        "systemctl",
        "service",
        "snap",
        "apt",
        "apt-cache",
        "dpkg",
        "dpkg-query",
        "pip",
        "npm",
        "git",
        "curl",
        "wget",
        "jq",
        "yq",
        "awk",
        "sed",
        "sort",
        "uniq",
        "cut",
        "tr",
        "diff",
        "md5sum",
        "sha256sum",
        "readlink",
        "realpath",
        "nproc",
        "getent",
        "timedatectl",
        "localectl",
        "loginctl",
        "zfs",
        "zpool",
        "smartctl",
    }
)

#: Subcommands that make an otherwise read-only binary a mutation.
_MUTATING_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "docker": frozenset(
        {
            "run",
            "start",
            "stop",
            "restart",
            "rm",
            "rmi",
            "exec",
            "pull",
            "build",
            "create",
            "kill",
            "prune",
            "compose",
            "network",
            "volume",
            "system",
            "update",
            "commit",
            "push",
            "tag",
            "load",
            "import",
        }
    ),
    "podman": frozenset({"run", "start", "stop", "restart", "rm", "rmi", "exec", "pull", "build"}),
    "systemctl": frozenset(
        {
            "start",
            "stop",
            "restart",
            "reload",
            "enable",
            "disable",
            "mask",
            "unmask",
            "kill",
            "isolate",
            "set-property",
            "daemon-reload",
            "poweroff",
            "reboot",
            "halt",
            "suspend",
            "hibernate",
        }
    ),
    "service": frozenset({"start", "stop", "restart", "reload"}),
    "apt": frozenset(
        {
            "install",
            "remove",
            "purge",
            "upgrade",
            "full-upgrade",
            "autoremove",
            "dist-upgrade",
            "update",
        }
    ),
    "snap": frozenset({"install", "remove", "refresh", "revert"}),
    "pip": frozenset({"install", "uninstall"}),
    "npm": frozenset({"install", "uninstall", "update", "publish"}),
    "git": frozenset(
        {
            "push",
            "reset",
            "clean",
            "checkout",
            "merge",
            "rebase",
            "commit",
            "pull",
            "fetch",
            "clone",
            "restore",
            "revert",
            "cherry-pick",
            "stash",
        }
    ),
    "zfs": frozenset({"destroy", "rollback", "create", "snapshot", "set", "rename"}),
    "zpool": frozenset({"destroy", "create", "remove", "replace", "offline", "online"}),
    "kubectl": frozenset(
        {
            "apply",
            "delete",
            "create",
            "patch",
            "scale",
            "rollout",
            "drain",
            "cordon",
            "exec",
            "edit",
        }
    ),
}

#: Subcommands that interrupt service or destroy data.
_DESTRUCTIVE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "systemctl": frozenset(
        {"poweroff", "reboot", "halt", "isolate", "mask", "kill", "suspend", "hibernate"}
    ),
    "docker": frozenset({"prune", "rmi", "rm", "kill"}),
    "apt": frozenset({"remove", "purge", "autoremove", "dist-upgrade", "full-upgrade"}),
    "zfs": frozenset({"destroy", "rollback"}),
    "zpool": frozenset({"destroy", "remove", "replace"}),
    "git": frozenset({"reset", "clean", "push"}),
    "kubectl": frozenset({"delete", "drain"}),
}

#: Binaries that are always destructive, and always need confirmation.
DESTRUCTIVE_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "shutdown",
        "reboot",
        "poweroff",
        "halt",
        "init",
        "kill",
        "killall",
        "pkill",
        "truncate",
        "shred",
        "unlink",
        "mv",
        "chown",
        "chmod",
        "chgrp",
        "umount",
        "mount",
        "swapoff",
        "iptables",
        "nft",
        "ufw",
        "crontab",
        "usermod",
        "groupmod",
    }
)

#: Never executed. These either destroy the machine or hand it to someone else.
FORBIDDEN_COMMANDS: frozenset[str] = frozenset(
    {
        "mkfs",
        "mkfs.ext4",
        "mkfs.xfs",
        "mkfs.btrfs",
        "fdisk",
        "sfdisk",
        "parted",
        "wipefs",
        "dd",
        "userdel",
        "deluser",
        "passwd",
        "chpasswd",
        "visudo",
        "su",
        "sudoedit",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "socat",
        "chroot",
        "insmod",
        "rmmod",
        "modprobe",
    }
)

#: Patterns that are refused wherever they appear in the command line.
FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:\s|$)"), "recursive delete of /"),
    # Only the system directory itself — `rm -rf /var/lib/thing` is ordinary
    # cleanup and stays merely destructive, needing confirmation rather than a ban.
    (
        re.compile(
            r"\brm\b[^|;&]*\s-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:etc|usr|bin|sbin|boot|var|lib|home)/?(?:\s|$)"
        ),
        "recursive delete of a system directory",
    ),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd)"), "raw write to a block device"),
    (re.compile(r"\bof=/dev/(sd|nvme|vd|hd)"), "raw write to a block device"),
    (re.compile(r":\(\)\s*\{.*\}\s*;?\s*:"), "fork bomb"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh"), "piping a download into a shell"),
    (re.compile(r"\bchmod\b\s+-R\s+777\s+/(?:\s|$)"), "world-writable root"),
    (re.compile(r"\b>\s*/etc/(passwd|shadow|sudoers)"), "overwriting a credential file"),
    (re.compile(r"\bhistory\s+-c\b|\bshred\b.*\.bash_history"), "history tampering"),
)

_SHELL_METACHARACTERS = re.compile(r"[|;&><`$(){}\[\]*?~\n]")


@dataclass(slots=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exitCode": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "durationMs": self.duration_ms,
            "truncated": self.truncated,
            "ok": self.ok,
        }

    def summarise(self, limit: int = 2000) -> str:
        """Compact form fed back to the model."""
        body = self.stdout.strip() or self.stderr.strip() or "(no output)"
        if len(body) > limit:
            body = body[:limit] + f"\n… truncated, {len(self.stdout)} bytes total"
        status = "ok" if self.ok else f"exit {self.exit_code}"
        return f"$ {self.command}\n[{status}]\n{body}"


@dataclass(slots=True)
class Classification:
    risk: Risk
    reason: str
    binary: str = ""
    uses_shell: bool = False


def classify(command: str, *, extra_readonly: frozenset[str] | None = None) -> Classification:
    """Assign a risk level to a command line."""
    stripped = command.strip()
    if not stripped:
        return Classification(Risk.FORBIDDEN, "empty command")

    for pattern, reason in FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            return Classification(Risk.FORBIDDEN, reason)

    uses_shell = bool(_SHELL_METACHARACTERS.search(stripped))

    try:
        tokens = shlex.split(stripped, comments=True)
    except ValueError as exc:
        return Classification(Risk.FORBIDDEN, f"unparseable command: {exc}")
    if not tokens:
        return Classification(Risk.FORBIDDEN, "empty command")

    # Evaluate every segment of a pipeline; the riskiest one wins.
    if uses_shell:
        segments = [s.strip() for s in re.split(r"\|\||&&|[|;&]", stripped) if s.strip()]
        if len(segments) > 1:
            worst = Classification(Risk.READ_ONLY, "pipeline", uses_shell=True)
            for segment in segments:
                part = classify(segment, extra_readonly=extra_readonly)
                if part.risk > worst.risk:
                    worst = Classification(part.risk, part.reason, part.binary, uses_shell=True)
            # A pipeline is never better than MUTATING: redirection can clobber files.
            if worst.risk is Risk.READ_ONLY:
                return Classification(Risk.MUTATING, "shell pipeline", tokens[0], uses_shell=True)
            return worst

    binary = os.path.basename(tokens[0])
    if binary in ("sudo", "doas", "pkexec"):
        if len(tokens) < 2:
            return Classification(Risk.FORBIDDEN, "bare privilege escalation")
        inner = classify(shlex.join(tokens[1:]), extra_readonly=extra_readonly)
        # Elevation raises the floor: a read-only command under sudo still counts
        # as a mutation, because it can read anything on the box.
        return Classification(
            max(inner.risk, Risk.MUTATING), f"elevated: {inner.reason}", inner.binary, uses_shell
        )

    if binary in FORBIDDEN_COMMANDS:
        return Classification(Risk.FORBIDDEN, f"'{binary}' is not permitted", binary, uses_shell)

    # Tools nest their verbs ("docker system prune", "kubectl rollout restart"), so
    # scan the leading non-flag tokens rather than just the first one — otherwise
    # `docker system prune` reads as the benign `docker system`.
    subcommands = [t for t in tokens[1:] if not t.startswith("-")][:3]
    subcommand = subcommands[0] if subcommands else ""

    if binary in _DESTRUCTIVE_SUBCOMMANDS:
        hit = next((s for s in subcommands if s in _DESTRUCTIVE_SUBCOMMANDS[binary]), "")
        if hit:
            return Classification(Risk.DESTRUCTIVE, f"{binary} {hit}", binary, uses_shell)
    if binary in DESTRUCTIVE_COMMANDS:
        return Classification(Risk.DESTRUCTIVE, f"'{binary}' can destroy data", binary, uses_shell)
    if binary in _MUTATING_SUBCOMMANDS and subcommand in _MUTATING_SUBCOMMANDS[binary]:
        return Classification(Risk.MUTATING, f"{binary} {subcommand}", binary, uses_shell)

    allowed = READ_ONLY_COMMANDS | (extra_readonly or frozenset())
    if binary in allowed:
        if uses_shell:
            return Classification(Risk.MUTATING, "shell redirection", binary, True)
        return Classification(Risk.READ_ONLY, "inspection", binary, False)

    return Classification(Risk.FORBIDDEN, f"'{binary}' is not in the allowlist", binary, uses_shell)


class CommandRunner:
    """Executes classified commands with timeouts and bounded output."""

    #: Hard cap on captured output; prevents a runaway `cat` from eating memory.
    MAX_OUTPUT = 256 * 1024

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        allow_shell: bool = False,
        extra_readonly: tuple[str, ...] = (),
        working_directory: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.allow_shell = allow_shell
        self.extra_readonly = frozenset(extra_readonly)
        self.working_directory = working_directory

    def classify(self, command: str) -> Classification:
        return classify(command, extra_readonly=self.extra_readonly)

    async def run(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Run a command that has already cleared the policy gate."""
        verdict = self.classify(command)
        if verdict.risk is Risk.FORBIDDEN:
            raise PermissionDenied(f"refused: {verdict.reason}")
        if verdict.uses_shell and not self.allow_shell:
            raise PermissionDenied(
                "shell pipelines and redirection are disabled — "
                "enable server.allow_shell to permit them"
            )
        return await self._execute(command, verdict, timeout or self.timeout)

    async def _execute(
        self, command: str, verdict: Classification, timeout: float
    ) -> CommandResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        # A non-interactive environment stops pagers and prompts from hanging us.
        env = {
            **os.environ,
            "DEBIAN_FRONTEND": "noninteractive",
            "PAGER": "cat",
            "SYSTEMD_PAGER": "",
        }

        try:
            if verdict.uses_shell:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                    env=env,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *shlex.split(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                    env=env,
                )
        except FileNotFoundError as exc:
            raise PermissionDenied(f"command not found: {exc.filename}") from exc
        except OSError as exc:
            raise PermissionDenied(f"could not start command: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return CommandResult(
                command=command,
                exit_code=124,
                stdout="",
                stderr=f"timed out after {timeout:.0f}s",
                duration_ms=int((loop.time() - started) * 1000),
            )

        truncated = len(stdout) > self.MAX_OUTPUT
        return CommandResult(
            command=command,
            exit_code=process.returncode or 0,
            stdout=stdout[: self.MAX_OUTPUT].decode("utf-8", "replace"),
            stderr=stderr[: self.MAX_OUTPUT].decode("utf-8", "replace"),
            duration_ms=int((loop.time() - started) * 1000),
            truncated=truncated,
        )
