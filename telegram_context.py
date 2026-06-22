"""Telegram -> Claude context pipeline (cross-platform).

Forward messages (text + screenshots) to the bot, then type a short note with
#tags. Within a debounce window everything is bundled into one markdown file,
with any screenshots downloaded into a ./media folder and embedded inline so
Claude can read them alongside the conversation.

Config is read from environment variables, optionally seeded by a .env file
sitting next to this script (see .env.example).
"""

import asyncio
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import MessageOriginType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    # Live synthesis is optional — never let an import problem take down the bot.
    from synthesize import synthesize, log_synthesis_startup_state
except Exception:  # pragma: no cover
    synthesize = None
    log_synthesis_startup_state = None

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Seed os.environ from a .env file next to this script.

    Tiny, dependency-free parser. Real environment variables win over .env, so
    a value already set in the shell / scheduled task is never overwritten.
    """
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# Tolerate an accidental inline comment or a non-numeric value rather than
# crashing at import (which, under pythonw, would be an invisible restart loop).
_debounce_raw = os.environ.get("TELEGRAM_DEBOUNCE_SECONDS", "90").split("#")[0].strip()
DEBOUNCE_SECONDS = int(_debounce_raw) if _debounce_raw.isdigit() else 90
FILE_PREFIX = os.environ.get("TELEGRAM_FILE_PREFIX", "feedback")
MEDIA_DIRNAME = "media"
MAX_SLUG_WORDS = 6

_default_output = Path.home() / "claude" / "context" / "feedback"
OUTPUT_DIR = Path(os.environ.get("TELEGRAM_OUTPUT_DIR") or _default_output).expanduser()

_default_log = SCRIPT_DIR / "logs" / "telegram-context.log"
LOG_PATH = Path(os.environ.get("TELEGRAM_LOG_PATH") or _default_log).expanduser()


def _allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


ALLOWED_USER_IDS = _allowed_ids()


@dataclass
class Buffer:
    messages: list = field(default_factory=list)
    flush_task: Optional[asyncio.Task] = None


BUFFERS: dict[int, Buffer] = defaultdict(Buffer)


def is_forwarded(msg) -> bool:
    return getattr(msg, "forward_origin", None) is not None


def origin_name(msg) -> str:
    fo = msg.forward_origin
    if fo is None:
        u = msg.from_user
        return u.full_name if u else "Unknown"
    t = fo.type
    if t == MessageOriginType.USER:
        return fo.sender_user.full_name
    if t == MessageOriginType.HIDDEN_USER:
        return f"{fo.sender_user_name} (hidden)"
    if t == MessageOriginType.CHANNEL:
        chat = fo.chat
        title = chat.title or chat.username or "channel"
        return f"{title} (channel)"
    if t == MessageOriginType.CHAT:
        sc = fo.sender_chat
        return (sc.title or sc.username or "group") + " (group)"
    return "Unknown"


def origin_date(msg) -> datetime:
    fo = msg.forward_origin
    return (fo.date if fo else msg.date).astimezone()


_IMAGE_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def _ext_from_mime(mime: Optional[str]) -> str:
    return _IMAGE_MIME_EXT.get((mime or "").lower(), ".img")


def message_image(msg) -> Optional[tuple[str, str]]:
    """Return (file_id, extension) for a downloadable image on this message.

    Covers photos and screenshots sent as image documents. Returns None for
    everything else (handled by media_marker instead).
    """
    if msg.photo:
        return msg.photo[-1].file_id, ".jpg"
    doc = msg.document
    if doc and (doc.mime_type or "").lower().startswith("image/"):
        # Only trust a known-good image extension; otherwise derive from MIME.
        # (A raw file name could carry a Windows-illegal char like ':' for an ADS.)
        allowed_ext = set(_IMAGE_MIME_EXT.values()) | {".jpeg"}
        candidate = Path(doc.file_name or "").suffix.lower()
        ext = candidate if candidate in allowed_ext else _ext_from_mime(doc.mime_type)
        return doc.file_id, ext
    return None


def media_marker(msg) -> Optional[str]:
    if msg.photo:
        return "[photo]"
    if msg.voice:
        return f"[voice {msg.voice.duration}s]"
    if msg.video:
        return f"[video {msg.video.duration}s]"
    if msg.video_note:
        return f"[video note {msg.video_note.duration}s]"
    if msg.animation:
        return "[gif]"
    if msg.document:
        return f"[document: {msg.document.file_name or 'file'}]"
    if msg.audio:
        title = msg.audio.title or msg.audio.file_name or "audio"
        return f"[audio: {title} {msg.audio.duration}s]"
    if msg.sticker:
        return f"[sticker {msg.sticker.emoji or ''}]".rstrip()
    if msg.poll:
        opts = "; ".join(o.text for o in msg.poll.options)
        return f'[poll: "{msg.poll.question}" ({opts})]'
    if msg.contact:
        c = msg.contact
        return f"[contact: {c.first_name} {c.last_name or ''}".rstrip() + "]"
    if msg.location:
        loc = msg.location
        return f"[location: {loc.latitude:.5f},{loc.longitude:.5f}]"
    if msg.dice:
        return f"[dice {msg.dice.emoji} = {msg.dice.value}]"
    return None


def format_message(msg, image_rel: Optional[str] = None) -> str:
    who = origin_name(msg)
    when = origin_date(msg).strftime("%Y-%m-%d %H:%M")
    parts = []
    if image_rel:
        parts.append(f"![screenshot]({image_rel})")
    else:
        marker = media_marker(msg)
        if marker:
            parts.append(marker)
    text = msg.text or msg.caption
    if text:
        parts.append(text.strip())
    if not parts:
        parts.append("[unsupported message type]")
    return f"**{who}** · {when}\n" + "\n".join(parts)


def extract_tags(text: str) -> list[str]:
    return [m.lower() for m in re.findall(r"#(\w+)", text or "")]


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*#\w+", "", text or "")).strip()


def slugify(text: str, max_words: int = MAX_SLUG_WORDS) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())[:max_words]
    return "-".join(words) or "untagged"


def unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    parent = base.parent
    i = 2
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def parse_bundle(messages: list):
    """Split a buffer into (context_msg, convo, tags, context_body).

    The last non-forwarded message is the user's synthesis note; everything else
    is the forwarded source material.
    """
    context_msg = next((m for m in reversed(messages) if not is_forwarded(m)), None)
    convo = [m for m in messages if m is not context_msg]
    raw_context = (context_msg.text or context_msg.caption or "").strip() if context_msg else ""
    tags = extract_tags(raw_context)
    context_body = strip_tags(raw_context)
    return context_msg, convo, tags, context_body


def bundle_stem(messages: list, now: datetime) -> str:
    """Filename stem (no extension): <timestamp>_<prefix>_<slug>."""
    _context_msg, convo, tags, context_body = parse_bundle(messages)
    slug_source = (
        "-".join(tags)
        if tags
        else slugify(context_body or (convo[0].text if convo and convo[0].text else "untagged"))
    )
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{FILE_PREFIX}_{slug_source}"


async def download_images(messages: list, stem: str, bot) -> dict:
    """Download photos / image documents into OUTPUT_DIR/media.

    Returns {id(msg): "media/<file>"} for messages whose image downloaded.
    A failed download is logged and simply omitted, so rendering falls back to
    the lightweight [photo] marker for that message.
    """
    image_map: dict = {}
    media_dir = OUTPUT_DIR / MEDIA_DIRNAME
    n = 0
    for m in messages:
        img = message_image(m)
        if not img:
            continue
        file_id, ext = img
        n += 1
        dest = media_dir / f"{stem}_{n}{ext}"
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
            tg_file = await bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=dest)
            image_map[id(m)] = f"{MEDIA_DIRNAME}/{dest.name}"
            logging.info("Downloaded screenshot -> %s", dest.name)
        except Exception as e:
            logging.warning("Screenshot download failed (%s): %s", dest.name, e)
    return image_map


def render_bundle(messages: list, now: datetime, image_map: dict) -> str:
    """Return the markdown content for a buffer of messages."""
    context_msg, convo, tags, context_body = parse_bundle(messages)
    participants = sorted({origin_name(m) for m in convo}) if convo else []

    if convo:
        span = (convo[-1].date - convo[0].date).total_seconds()
        window = int(max(span, 0))
    else:
        window = 0

    fm = [
        "---",
        f"date: {now.isoformat(timespec='seconds')}",
        "source: telegram",
        f"tags: [{', '.join(tags)}]",
        f"participants: [{', '.join(participants)}]",
        f"forward_count: {len(convo)}",
        f"screenshot_count: {len(image_map)}",
        f"window_seconds: {window}",
        "---",
        "",
    ]

    body: list[str] = []
    context_has_image = bool(context_msg and id(context_msg) in image_map)
    context_image_failed = bool(
        context_msg and message_image(context_msg) and id(context_msg) not in image_map
    )
    if context_body or context_has_image or context_image_failed:
        body += ["# Context", ""]
        if context_body:
            body += [context_body, ""]
        if context_has_image:
            body += [f"![screenshot]({image_map[id(context_msg)]})", ""]
        elif context_image_failed:
            marker = media_marker(context_msg)
            if marker:
                body += [marker, ""]
    if convo:
        body += ["# Conversation", ""]
        for m in convo:
            body.append(format_message(m, image_map.get(id(m))))
            body.append("")
    if not body:
        body += ["# (empty bundle)", ""]

    return "\n".join(fm) + "\n".join(body)


async def flush(chat_id: int, bot) -> None:
    buf = BUFFERS.pop(chat_id, None)
    if not buf or not buf.messages:
        return

    messages = buf.messages
    now = datetime.now().astimezone()
    stem = bundle_stem(messages, now)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = unique_path(OUTPUT_DIR / f"{stem}.md")
    final_stem = md_path.stem

    image_map = await download_images(messages, final_stem, bot)
    content = render_bundle(messages, now, image_map)
    md_path.write_text(content, encoding="utf-8")
    logging.info(
        "Flushed %d msg(s), %d screenshot(s) -> %s",
        len(messages),
        len(image_map),
        md_path.name,
    )

    shots = len(image_map)
    note = f" ({shots} screenshot{'s' if shots != 1 else ''})" if shots else ""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"✓ filed `{md_path.name}`{note}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning("Confirmation reply failed: %s", e)

    # Fold the new item into the rolling synthesis and report the verdict. Runs
    # after the filing confirmation since it may take a few seconds; best-effort.
    if synthesize is not None:
        try:
            verdict = await synthesize(OUTPUT_DIR, content, md_path.name)
            if verdict:
                await bot.send_message(chat_id=chat_id, text=verdict)
        except Exception as e:
            logging.warning("Synthesis step failed: %s", e)


async def debounce_and_flush(chat_id: int, bot) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await flush(chat_id, bot)
    except asyncio.CancelledError:
        pass


def is_allowed(msg) -> bool:
    if not ALLOWED_USER_IDS:
        return False
    u = msg.from_user
    return bool(u and u.id in ALLOWED_USER_IDS)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None:
        return
    if not is_allowed(msg):
        uid = msg.from_user.id if msg.from_user else "?"
        logging.info("Ignored message from non-allowed user id=%s", uid)
        try:
            await msg.reply_text(
                f"Not authorized. Your Telegram user ID is `{uid}`. "
                "Add it to TELEGRAM_ALLOWED_USER_IDS and restart the bot.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    chat_id = msg.chat_id
    buf = BUFFERS[chat_id]
    buf.messages.append(msg)
    if buf.flush_task:
        buf.flush_task.cancel()
    buf.flush_task = asyncio.create_task(debounce_and_flush(chat_id, context.bot))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u is None:
        return
    # Always echo the caller their own id (needed for onboarding), but only
    # disclose the whole allow-list to someone already whitelisted.
    text = f"Your Telegram user ID is `{u.id}`."
    if is_allowed(update.message):
        text += f"\nAllowed currently: {sorted(ALLOWED_USER_IDS) or '∅'}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_flush(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.message):
        return
    chat_id = update.message.chat_id
    buf = BUFFERS.get(chat_id)
    if buf and buf.flush_task:
        buf.flush_task.cancel()
    await flush(chat_id, context.bot)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Forward the messages you want to capture (text and/or screenshots), then "
        f"type a short note (optionally with #tags). After {DEBOUNCE_SECONDS}s "
        "of silence the bundle is filed and any screenshots are saved alongside it.\n\n"
        "Commands:\n"
        "/id — show your Telegram user ID\n"
        "/flush — file the current buffer immediately\n"
        "/help — this message"
    )


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set (put it in .env or the environment)", file=sys.stderr)
        sys.exit(1)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # pythonw.exe (the scheduled-task runner) has no console: sys.stderr is None,
    # so a StreamHandler would error on every log call. Only attach it when a real
    # stream exists (the foreground run.bat path). The file log always works.
    log_handlers: list = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if sys.stderr is not None:
        log_handlers.append(logging.StreamHandler())
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        handlers=log_handlers,
    )

    if not ALLOWED_USER_IDS:
        logging.warning(
            "TELEGRAM_ALLOWED_USER_IDS not set - bot will reject all messages. "
            "Send /id to the bot to discover your ID, then add it and restart."
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("flush", cmd_flush))
    app.add_handler(CommandHandler(["help", "start"], cmd_help))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    logging.info(
        "Bot up. debounce=%ss output=%s allowed=%s",
        DEBOUNCE_SECONDS,
        OUTPUT_DIR,
        sorted(ALLOWED_USER_IDS) or "none",
    )
    if log_synthesis_startup_state is not None:
        log_synthesis_startup_state()
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
