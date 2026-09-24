"""Which reasoning model handles a turn.

Everyday commands run on the fast OpenAI model; coding, controlling the
machine, and genuinely involved requests are escalated to Claude, which is
stronger at exactly those. Two rules keep the split honest:

* It never escalates unless the advanced tier is actually configured — a blank
  Anthropic key means every turn stays on OpenAI, exactly as before.
* When it isn't sure, it stays on the cheaper model. Escalation is not free
  (latency and cost), so the signals below are chosen to fire on requests that
  clearly benefit from the stronger model, not on any request that merely
  mentions a file or a light.

An explicit ask always wins over the heuristic — say "think hard about this"
or "ask Claude" and it escalates even with auto-routing turned off.
"""

from __future__ import annotations

import re

#: Phrases that ask for the stronger model outright. These override everything:
#: they fire even when auto-routing is off, because the user asked directly.
_EXPLICIT = (
    "think hard",
    "think carefully",
    "think really hard",
    "reason carefully",
    "reason through",
    "take your time",
    "use claude",
    "ask claude",
    "use the advanced",
    "advanced reasoning",
    "work through this carefully",
)

#: Coding / technical work — the clearest win for the advanced tier.
_CODING = (
    "code",
    "coding",
    "python",
    "javascript",
    "typescript",
    "rust",
    "golang",
    "bash script",
    "shell script",
    "function",
    "regex",
    "regular expression",
    "stack trace",
    "traceback",
    "compile",
    "compiler",
    "syntax error",
    "refactor",
    "debug",
    "algorithm",
    "sql query",
    "write a program",
    "write a script",
    "git ",
    "pull request",
)

#: Controlling the machine. Kept to multi-word phrases so a bare "open the
#: front door" (a camera/home request) does not escalate — only phrasing that
#: really is about operating the computer does.
_SYSTEM = (
    "on my computer",
    "on my pc",
    "on my machine",
    "my files",
    "a file called",
    "the file ",
    "in the terminal",
    "run the command",
    "run a command",
    "command line",
    "install ",
    "uninstall ",
    "create a folder",
    "create a file",
    "delete the file",
    "rename the file",
    "move the file",
    "open the app",
    "launch the app",
    "change the setting",
    "change my settings",
)

#: Genuinely complex requests — multi-step reasoning, analysis, planning.
_COMPLEX = (
    "step by step",
    "step-by-step",
    "figure out",
    "work out how",
    "come up with a plan",
    "make a plan",
    "plan out",
    "analyse",
    "analyze",
    "compare and contrast",
    "pros and cons",
    "explain why",
    "walk me through",
    "troubleshoot",
)

#: Above this word count a request is long enough that the stronger model is
#: worth it regardless of topic — short commands are what the fast tier is for.
_LONG_REQUEST_WORDS = 40


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def wants_advanced(text: str, *, configured: bool, auto_route: bool) -> bool:
    """True when this turn should use the advanced (Claude) tier.

    ``configured`` is whether an Anthropic key is set; ``auto_route`` is the
    user's setting for automatic escalation. An explicit request in the text
    escalates even when ``auto_route`` is off, but nothing escalates when the
    tier is not configured at all.
    """
    if not configured:
        return False

    lowered = text.lower()
    if _contains_any(lowered, _EXPLICIT):
        return True
    if not auto_route:
        return False

    if _contains_any(lowered, _CODING):
        return True
    if _contains_any(lowered, _SYSTEM):
        return True
    if _contains_any(lowered, _COMPLEX):
        return True

    # Long, elaborate requests tend to be the ones worth the stronger model,
    # even when they trip none of the keyword lists above.
    return len(re.findall(r"\w+", lowered)) >= _LONG_REQUEST_WORDS
