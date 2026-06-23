# telegram-context

[![tests](https://github.com/bsnydr/telegram-context/actions/workflows/tests.yml/badge.svg)](https://github.com/bsnydr/telegram-context/actions/workflows/tests.yml)

Forward Telegram messages to a private bot, add a short tagged note, and get one markdown file written to a folder Claude Code can read. Cross-platform (macOS and Windows).

## Why this exists

Useful context arrives as chat. A teammate forwards three messages about a broken signup flow, you remember a decision from a thread last week, someone pastes a screenshot of an error. None of it is in a form an AI coding assistant can use. It lives in Telegram, untagged and ungreppable.

This bot turns a forwarded batch plus a one-line note into a single markdown file with YAML frontmatter, a `# Context` block (your note), and a `# Conversation` block (the forwarded messages). Files land in a folder you point Claude Code at. Ad-hoc chat becomes tagged, searchable context that survives past the scrollback.

It is single-purpose and personal: one bot, an allow-list of numeric Telegram user IDs, no database, no server beyond the polling process.

## Quickstart

### Prerequisites

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather) (send `/newbot`, copy the token)
- Optional: the Claude Code CLI on PATH, if you want live synthesis (see below)

### Create the bot

In Telegram, message `@BotFather`, send `/newbot`, follow the prompts, and copy the token it gives you. It looks like `1234567890:ABC...`. Keep it private; treat it like a password.

### macOS

The token and allow-list come from the macOS Keychain. Nothing secret touches the repo.

```sh
# install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# store secrets in Keychain (one time)
security add-generic-password -a "$USER" -s telegram-bot-token -w "PASTE_YOUR_TOKEN"
security add-generic-password -a "$USER" -s telegram-allowed-ids -w ""   # fill in after first /id

# run in the foreground to test
./run.sh
```

On startup with an empty allow-list, the bot logs a warning and rejects everything. Message your bot `/id`, copy the number it replies with, store it in Keychain, and restart:

```sh
security add-generic-password -U -a "$USER" -s telegram-allowed-ids -w "123456789"
```

To run at login, edit `com.telegram-context.plist` (replace the placeholder path with the absolute path to `run.sh` in your checkout), then:

```sh
cp com.telegram-context.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.telegram-context.plist
```

### Windows

The token and allow-list come from a gitignored `.env` file.

```bat
:: first-run: create .venv and install dependencies
setup.bat

:: create your config
copy .env.example .env
:: then edit .env: paste TELEGRAM_BOT_TOKEN, leave TELEGRAM_ALLOWED_USER_IDS blank for now

:: run in the foreground to test
run.bat
```

Message your bot `/id`, copy the number, put it in `.env` as `TELEGRAM_ALLOWED_USER_IDS=123456789`, and restart. To run at logon as a background task (uses `pythonw.exe`, no console window):

```powershell
powershell -ExecutionPolicy Bypass -File install-task.ps1
```

`uninstall-task.ps1` removes it.

### Expected output

Forward a couple of messages to the bot, then type a note like:

```
the onboarding thread re: signup flow #onboarding #signup
```

After the debounce window (90s of silence by default), the bot replies `✓ filed 20260622_143012_feedback_onboarding-signup.md` and writes that file to your output folder:

```markdown
---
date: 2026-06-22T14:30:12+02:00
source: telegram
tags: [onboarding, signup]
participants: [Jane Doe]
forward_count: 2
screenshot_count: 0
window_seconds: 60
---

# Context

the onboarding thread re: signup flow

# Conversation

**Jane Doe** · 2026-06-22 14:21
the new users are bouncing on the email step

**Jane Doe** · 2026-06-22 14:22
three of them mentioned the verification link never arrived
```

If you forwarded screenshots, they download into a `media/` subfolder and embed inline as `![screenshot](media/...)`.

## How it works

The flow is: forward messages, type a `#tagged` note, wait out the debounce, get one filed markdown bundle.

```
Telegram --forward--> bot (long-poll) --> per-chat buffer
                                              |
                              90s of silence (debounce)
                                              |
                                              v
                        download screenshots -> media/
                        render markdown (frontmatter + Context + Conversation)
                        write <timestamp>_<prefix>_<slug>.md
                                              |
                                  (optional) fold into SYNTHESIS.md
```

Components, all in one cross-platform Python core:

- **Debounce buffer.** Each incoming message is appended to a per-chat buffer and a flush is scheduled `DEBOUNCE_SECONDS` out. Every new message cancels the pending flush and reschedules it. The buffer only flushes after a quiet window, so a burst of forwards plus a trailing note become one file, not ten. `/flush` forces it immediately.
- **Bundle parsing.** The last non-forwarded message in a buffer is treated as your note; everything else is source material. `#tags` are extracted from the note into frontmatter and used to build the filename slug; the note text with tags stripped becomes the `# Context` block.
- **Message rendering.** Forwarded messages keep their origin name and timestamp (resolved from `forward_origin`, including hidden users, channels, and groups). Photos and image documents download and embed inline; other media (voice, video, polls, locations, documents) render as lightweight text markers like `[voice 12s]`.
- **Allow-list gating.** Every update is checked against `TELEGRAM_ALLOWED_USER_IDS`. An unknown sender gets told their own ID and is otherwise ignored. With no allow-list set, the bot rejects everything by default.
- **Optional live synthesis.** After each filing, the bundle can be folded into a rolling `SYNTHESIS.md` and the bot replies with a duplicate / conflict / new verdict (see below).

The two operating systems differ only in secret storage (Keychain vs `.env`) and run-at-login mechanism (launchd vs Task Scheduler). The bot logic is identical; both read the same environment variables.

## Live synthesis (optional)

If you have the Claude Code CLI installed, set `TELEGRAM_SYNTHESIS=1`. After each bundle is filed, `synthesize.py` shells out to `claude -p` as a pure text-in / JSON-out transformer: it passes the current `SYNTHESIS.md` plus the new bundle, gets back a clustered, deduplicated synthesis document, writes it, and replies in Telegram with a one-line verdict (e.g. "duplicate of the email-verification theme, now 4 reports").

Design constraints that matter here:

- **It never blocks filing.** The raw markdown is always written first and the `✓ filed` confirmation is sent before synthesis runs. Synthesis is best-effort: every failure mode (CLI missing, timeout, malformed output, non-zero exit) is logged and skipped. A broken synthesis step never costs you a captured bundle.
- **Model output is treated as pure data.** `claude -p` is invoked with the prompt on stdin and JSON on stdout; the bot parses that JSON and writes the file itself. It never executes tool calls or acts on whatever the model returns, so a misbehaving synthesis pass can corrupt a verdict line, never the host. (For a hard guarantee at the process level, pass the CLI a tool-restricting flag such as `--allowed-tools ""`; the current build relies on the bot ignoring tool output rather than on the flag.)
- **Hardened subprocess.** A synthesis run that exceeds the timeout is killed by process tree (`taskkill /T` on Windows so the `node` grandchild behind the `.cmd` shim is not orphaned; `proc.kill()` elsewhere), then reaped. A lock serializes runs so two near-simultaneous flushes can't race on `SYNTHESIS.md`.
- **CLI discovery self-heals.** A Windows Scheduled Task at logon may not inherit the user PATH, so resolution falls back to known install locations (`%APPDATA%\npm`, `%USERPROFILE%\.local\bin`) before giving up. Set `TELEGRAM_CLAUDE_BIN` to an absolute path to skip the guessing.

## Retrieving context in Claude Code

Point Claude Code at the output folder, or narrow it:

```sh
# everything captured
claude "read ~/claude/context and summarize the open onboarding issues"

# one tag
grep -rl "tags:.*signup" ~/claude/context

# the rolling synthesis, if live synthesis is on
claude "read ~/claude/context/SYNTHESIS.md and tell me the top three themes"
```

Because tags live in both frontmatter and the filename, `grep` and glob both work without any index.

## Configuration reference

All configuration is environment variables. On Windows they are typically set in `.env`; on macOS `run.sh` exports them from Keychain.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | BotFather token. Never logged. |
| `TELEGRAM_ALLOWED_USER_IDS` | (empty) | Comma-separated numeric Telegram user IDs. Empty = reject all. |
| `TELEGRAM_OUTPUT_DIR` | `~/claude/context/feedback` | Where bundles and `media/` are written. |
| `TELEGRAM_DEBOUNCE_SECONDS` | `90` | Quiet window before a buffer flushes. Tolerates an inline comment. |
| `TELEGRAM_FILE_PREFIX` | `feedback` | Middle segment of the filename. |
| `TELEGRAM_LOG_PATH` | `logs/telegram-context.log` | Log file. A console handler is added only when a real stderr exists. |
| `TELEGRAM_SYNTHESIS` | `1` | `0`/`false`/`off` disables live synthesis. |
| `TELEGRAM_CLAUDE_BIN` | `claude` | Path to the Claude Code CLI. |
| `TELEGRAM_SYNTHESIS_MODEL` | (CLI default) | `opus` / `sonnet` / `haiku`, or blank. |
| `TELEGRAM_SYNTHESIS_TIMEOUT` | `120` | Seconds before a synthesis run is killed. |

## Daily use

- **Forward** the messages you want captured (text and/or screenshots).
- **Type a note**, optionally with `#tags`. This becomes the `# Context` block and seeds the filename.
- Wait out the debounce, or send `/flush` to file immediately.
- `/id` shows your Telegram user ID (and, if you're whitelisted, the current allow-list).
- `/help` prints the usage summary.

## Security model

- The token is never hardcoded and never logged: macOS Keychain or a gitignored `.env`.
- Access is gated by an explicit allow-list of numeric Telegram user IDs. No allow-list means no access; an unknown sender only ever learns their own ID.
- `/id` reveals the full allow-list only to users already on it.
- Nothing sensitive is committed. `.gitignore` covers `.venv/`, `__pycache__/`, `*.pyc`, `.env`, `logs/`, `media/`, and `.DS_Store`.
- The optional synthesis subprocess is fed a prompt and returns JSON; the bot treats that output as pure data and does the write itself, so nothing the model emits is executed on the host.

## Key technical decisions and tradeoffs

- **Long-polling, not webhooks.** No public endpoint, no TLS, no inbound firewall hole. This is a single-user tool that runs on a laptop or a small box. Polling is the right amount of infrastructure for that. The tradeoff is one always-on process; the launchd / Task Scheduler integration exists to keep it alive across reboots.

- **Debounce instead of an explicit "done" command.** Capturing context should feel like forwarding, not like filling a form. Debounce lets a natural burst (several forwards, then a note) collapse into one file with zero ceremony. The cost is latency: the file appears 90s after you stop. `/flush` is the escape hatch when you want it now.

- **Markdown files, no database.** The output is the interface. Frontmatter plus tags in the filename means `grep`, glob, and any editor work with no schema and no query layer. A captured bundle is readable and useful even if this bot is never run again. The cost is no relational queries and no dedup across files except via the optional synthesis pass.

- **Synthesis is strictly best-effort and runs after filing.** The expensive, failure-prone step (an LLM subprocess) is decoupled from the cheap, must-not-fail step (writing the file). Filing is durable; synthesis is a bonus. This is why every synthesis failure is a logged warning rather than an error path, and why the file is on disk before `claude -p` is ever invoked.

- **Dependency-free `.env` loader.** The only runtime dependency is `python-telegram-bot`. The `.env` parser is about 15 lines and real environment variables always win over `.env`, so a value set by the shell or scheduled task is never clobbered. Not worth pulling in `python-dotenv` for this.

- **`pythonw`-safe logging.** Run headless on Windows, `sys.stderr` is `None`, so a `StreamHandler` would raise on every log call and the process would restart-loop invisibly. The bot attaches the stream handler only when a real stderr exists; the file log always works. This kind of headless edge case is the sort of thing that only shows up in production.

- **Image extension hardening.** Screenshot extensions are derived from the known-good MIME map, not trusted from the sender's filename, which could carry a Windows-illegal character (e.g. a `:` triggering an alternate data stream). A failed download degrades to the `[photo]` marker rather than dropping the message.

**What I'd improve.** The output directory is single and global, so two unrelated tag families share one folder; per-tag routing or per-project output dirs would scale better. Synthesis re-sends the entire `SYNTHESIS.md` on every item, which is fine at small volume but grows linearly; chunking or a summarize-then-append strategy would cap the prompt size. There is no reverse channel: the bot can't be queried from Telegram, only fed. And media beyond images is captured as text markers only, not downloaded.

## Limitations / scope

This is a v1 personal tool. Deliberately out of scope:

- Single output folder; no per-tag or per-project routing.
- Only images are downloaded and embedded; other media is a text marker.
- No reverse channel: you forward to it, you don't query it from Telegram.
- One bot, one user (the allow-list can hold several, but there's no multi-tenancy).
- Synthesis prompt grows with the synthesis document; not tuned for high volume.

## Project layout

```
telegram_context.py          shared cross-platform bot core
synthesize.py                optional live synthesis (claude -p)
requirements.txt             python-telegram-bot==21.10
.env.example                 config template (Windows / generic)
.gitignore
run.sh                       macOS: Keychain -> env -> exec the bot
com.telegram-context.plist   macOS: launchd run-at-login (edit the path)
setup.bat                    Windows: create .venv, install requirements
run.bat                      Windows: foreground run for testing
install-task.ps1             Windows: register the logon Scheduled Task
uninstall-task.ps1           Windows: remove it
```

## Troubleshooting

- **Bot ignores you / "Not authorized".** Your ID isn't on the allow-list. Send `/id`, then add the number to `TELEGRAM_ALLOWED_USER_IDS` (Windows) or the Keychain `telegram-allowed-ids` entry (macOS) and restart.
- **Nothing gets filed.** Check the service is running (`launchctl list | grep telegram-context` on macOS; Task Scheduler on Windows) and tail the log (`TELEGRAM_LOG_PATH`, default `logs/telegram-context.log`).
- **Synthesis never fires.** The log line at startup says whether synthesis is on and whether the `claude` CLI was found. If not found, set `TELEGRAM_CLAUDE_BIN` to the absolute path (`which claude` / `where claude`), or set `TELEGRAM_SYNTHESIS=0` to silence it.
- **Token rotation.** Get a fresh token from BotFather, replace it in Keychain or `.env`, and restart. The old token stops working immediately.

## License

MIT.
