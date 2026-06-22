"""Live synthesis via Claude Code headless (`claude -p`).

After each feedback bundle is filed, fold it into a rolling SYNTHESIS.md in the
output folder: cluster themes, count repeats, and flag conflicts. Returns a
one-line verdict for the Telegram reply.

This shells out to the `claude` CLI with the prompt on stdin and JSON on stdout.
The model's reply is treated as pure data: the bot parses the JSON and writes the
file itself, so nothing the model returns is ever executed. Every failure mode
(claude missing, timeout, bad output) is logged and skipped; the raw bundle is
always filed regardless of what happens here.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_truthy = os.environ.get("TELEGRAM_SYNTHESIS", "1").strip().lower()
SYNTHESIS_ENABLED = _truthy not in ("0", "false", "no", "off", "")

CLAUDE_BIN = os.environ.get("TELEGRAM_CLAUDE_BIN", "claude")
# Blank = use Claude Code's own default model. Or "opus" / "sonnet" / "haiku".
SYNTHESIS_MODEL = os.environ.get("TELEGRAM_SYNTHESIS_MODEL", "").strip()

_to_raw = os.environ.get("TELEGRAM_SYNTHESIS_TIMEOUT", "120").split("#")[0].strip()
SYNTHESIS_TIMEOUT = int(_to_raw) if (_to_raw.isdigit() and int(_to_raw) > 0) else 120

SYNTHESIS_FILENAME = "SYNTHESIS.md"

# Serialize synthesis so two near-simultaneous flushes can't race on the file.
_lock = asyncio.Lock()
_state_logged = False


INSTRUCTIONS = """You maintain a rolling synthesis of product feedback for a team.

You are given (1) the CURRENT SYNTHESIS document and (2) ONE NEW FEEDBACK ITEM just received. Fold the new item into the synthesis.

Decide how the new item relates to what is already recorded:
- "duplicate": it restates a theme already present (the same underlying request or issue, even if worded differently). Increment that theme's count and add the new item as evidence.
- "conflict": it contradicts or pulls against an existing theme or earlier report (e.g. one user wants X simpler, another wants more options in X). Keep both, and record the tension under the relevant theme and in the "Open conflicts" list.
- "new": it raises a theme not yet present. Add it.
- "mixed": more than one of the above.

Keep the synthesis CONCISE and CLUSTERED — summarize, do not paste raw feedback. Always cite the source bundle filename(s) as evidence so a reader can trace back. List themes most-reported first.

Use exactly this structure for the synthesis document:

# Product feedback synthesis

## Open conflicts
- <one line per unresolved tension, naming the themes/files involved> (write "None" if there are none)

## Themes
### <Theme title> — <N> report(s)
<one or two line summary>
- evidence: <bundle filename>, <bundle filename>, ...
- conflict: <describe any tension, only if applicable>

Then respond with ONLY a JSON object — no prose, no markdown fences — of exactly this shape:
{"classification": "duplicate|conflict|new|mixed", "verdict": "<one short line for a Telegram reply, ~120 chars max, may start with an emoji such as the recycle, warning, or sparkle symbol>", "synthesis_md": "<the full updated synthesis document as markdown>"}
"""


def _build_prompt(current: str, name: str, bundle: str) -> str:
    return (
        INSTRUCTIONS
        + "\n\n=== CURRENT SYNTHESIS ===\n"
        + current
        + "\n\n=== NEW FEEDBACK ITEM (filename: "
        + name
        + ") ===\n"
        + bundle
        + "\n"
    )


def _parse_model_json(text: str) -> Optional[dict]:
    """Parse the model's response into a dict, tolerating ``` fences or stray prose."""
    t = text.strip()
    if t.startswith("```"):
        # drop the opening fence line and any trailing fence
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if "```" in t:
            t = t[: t.rfind("```")]
        t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        a, b = t.find("{"), t.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(t[a : b + 1])
            except Exception:
                return None
        return None


def _resolve_claude() -> Optional[str]:
    """Find the claude CLI: PATH first, then the known default install locations.

    The CLI lives on the *user* PATH, which a Windows Scheduled Task may not inherit
    at logon — so probe the standard install paths too, letting it self-heal without
    the teammate having to set TELEGRAM_CLAUDE_BIN. (An absolute TELEGRAM_CLAUDE_BIN
    is still the most reliable; shutil.which() accepts an absolute path as-is.)
    """
    found = shutil.which(CLAUDE_BIN)
    if found:
        return found
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "claude.cmd")  # npm global install
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / ".local" / "bin" / "claude.exe")  # native installer
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _log_state_once(bin_path: Optional[str]) -> None:
    """Emit one authoritative line about synthesis wiring (so the log confirms it)."""
    global _state_logged
    if _state_logged:
        return
    _state_logged = True
    if bin_path:
        logging.info(
            "Synthesis ON, claude=%s (model=%s, timeout=%ss)",
            bin_path, SYNTHESIS_MODEL or "(default)", SYNTHESIS_TIMEOUT,
        )
    else:
        logging.warning(
            "Synthesis is ON but the 'claude' CLI was not found on the task PATH or in "
            "the default install locations - skipping synthesis. Set TELEGRAM_CLAUDE_BIN "
            "to its absolute path (run 'where claude'), or TELEGRAM_SYNTHESIS=0 to silence."
        )


def log_synthesis_startup_state() -> None:
    """Called once at bot startup so the resolved state shows up next to 'Bot up.'"""
    if not SYNTHESIS_ENABLED:
        logging.info("Synthesis OFF (TELEGRAM_SYNTHESIS=0)")
        return
    _log_state_once(_resolve_claude())


def _run_claude(prompt: str) -> Optional[dict]:
    """Blocking: run `claude -p`, return {classification, verdict, synthesis_md} or None."""
    bin_path = _resolve_claude()
    _log_state_once(bin_path)
    if bin_path is None:
        return None

    args = [bin_path, "-p", "--output-format", "json"]
    if SYNTHESIS_MODEL:
        args += ["--model", SYNTHESIS_MODEL]
    # On Windows the npm-installed CLI is a .cmd/.ps1 shim; run it through cmd.
    # All argv tokens here are fixed and shell-safe; the prompt goes via stdin.
    if sys.platform == "win32" and bin_path.lower().endswith((".cmd", ".bat", ".ps1")):
        args = ["cmd", "/c", *args]

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except Exception as e:
        logging.warning("Synthesis subprocess failed to start: %s", e)
        return None

    try:
        out, err = proc.communicate(input=prompt, timeout=SYNTHESIS_TIMEOUT)
    except subprocess.TimeoutExpired:
        logging.warning("Synthesis timed out after %ss - killing process tree.", SYNTHESIS_TIMEOUT)
        if sys.platform == "win32":
            # proc.kill() only terminates the cmd.exe shim; taskkill /T also kills
            # the claude/node grandchild it spawned (otherwise it would be orphaned).
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        try:
            proc.communicate(timeout=5)  # reap so we don't leave a zombie
        except Exception:
            pass
        return None
    except Exception as e:
        logging.warning("Synthesis subprocess failed: %s", e)
        try:
            proc.kill()
        except Exception:
            pass
        return None

    if proc.returncode != 0:
        logging.warning("claude -p exited %s: %s", proc.returncode, (err or "").strip()[:300])
        return None

    try:
        env = json.loads(out)
    except Exception as e:
        logging.warning("Could not parse claude output envelope: %s", e)
        return None

    if env.get("is_error"):
        logging.warning("claude reported an error: %s", str(env.get("result", ""))[:200])
        return None

    data = _parse_model_json(str(env.get("result") or ""))
    if not data or "synthesis_md" not in data or "verdict" not in data:
        logging.warning("Synthesis output missing required fields - skipping.")
        return None
    return data


async def synthesize(output_dir: Path, new_bundle_md: str, new_bundle_name: str) -> Optional[str]:
    """Fold a new bundle into SYNTHESIS.md. Returns a one-line verdict, or None if skipped."""
    if not SYNTHESIS_ENABLED:
        return None

    async with _lock:
        synth_path = output_dir / SYNTHESIS_FILENAME
        try:
            current = synth_path.read_text(encoding="utf-8") if synth_path.exists() else "(empty - no themes recorded yet)"
        except Exception as e:
            logging.warning("Could not read %s: %s", SYNTHESIS_FILENAME, e)
            current = "(empty - no themes recorded yet)"

        prompt = _build_prompt(current, new_bundle_name, new_bundle_md)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _run_claude, prompt)
        if not data:
            return None

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            synth_path.write_text(data["synthesis_md"], encoding="utf-8")
        except Exception as e:
            logging.warning("Failed writing %s: %s", SYNTHESIS_FILENAME, e)
            return None

        verdict = (data.get("verdict") or "").strip()
        logging.info("Synthesis [%s]: %s", data.get("classification", "?"), verdict or "(updated)")
        return verdict or None
