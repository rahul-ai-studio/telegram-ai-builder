"""Telegram AI Builder Bot — Main entry point.

SECURITY MODEL
──────────────
Layer 1 — chat_id whitelist (owner_only decorator)
  Every single handler checks the incoming chat_id against TELEGRAM_CHAT_ID
  from .env before doing anything. Strangers receive NO response (silent drop).
  The bot refuses to start if TELEGRAM_CHAT_ID is not set.

Layer 2 — PIN session (require_session decorator)
  If BOT_SECRET_PIN is set in .env, every handler (except /auth and /lock)
  also requires an active time-limited session. The user must type:
      /auth <pin>
  The PIN message is immediately auto-deleted so it never persists in
  Telegram chat history. Sessions live in memory only and are wiped on
  bot restart (restart = instant lock). A failed PIN attempt is rate-limited
  (max 3 tries per 30 minutes). Use /lock to manually revoke the session.

These two layers together mean:
  • A stranger who finds the bot gets zero response.
  • A hacker who compromises your Telegram account cannot use the bot
    without knowing the PIN (which is only in .env on your local machine,
    never in any Telegram message).
"""

from __future__ import annotations

import logging
import os
import time
from functools import wraps

import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import memory
from builder import build_project, qa_review
from media import analyze_image, analyze_pdf
from prompts import SYSTEM_CHAT
from router import MODEL_SONNET, route_model, estimate_cost, get_model_alias

# ──────────────────────────────────────────────
# Bootstrap
# ──────────────────────────────────────────────

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "").strip()
OWNER_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID",   "").strip()
BOT_SECRET_PIN     = os.getenv("BOT_SECRET_PIN",     "").strip()
SESSION_TTL        = int(os.getenv("SESSION_TTL_HOURS", "24")) * 3600

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# In-memory session store  (wiped on restart)
# ──────────────────────────────────────────────

_sessions:      dict[str, float] = {}   # chat_id  → expiry epoch
_auth_failures: dict[str, list]  = {}   # chat_id  → [failure epoch, ...]

_MAX_AUTH_FAILURES    = 3
_AUTH_LOCKOUT_SECONDS = 1800   # 30 minutes


def _session_active(chat_id: str) -> bool:
    return time.time() < _sessions.get(chat_id, 0)


def _grant_session(chat_id: str) -> None:
    _sessions[chat_id] = time.time() + SESSION_TTL


def _revoke_session(chat_id: str) -> None:
    _sessions.pop(chat_id, None)


def _auth_allowed(chat_id: str) -> bool:
    """True if the user has not exceeded the failure rate limit."""
    now = time.time()
    recent = [t for t in _auth_failures.get(chat_id, [])
              if now - t < _AUTH_LOCKOUT_SECONDS]
    _auth_failures[chat_id] = recent
    return len(recent) < _MAX_AUTH_FAILURES


def _record_auth_failure(chat_id: str) -> None:
    _auth_failures.setdefault(chat_id, []).append(time.time())


# ──────────────────────────────────────────────
# Security decorators
# ──────────────────────────────────────────────

def owner_only(func):
    """Layer 1 — reject every chat that is not the registered owner.
    Unknown users receive ZERO response (silent drop).
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        incoming = str(update.effective_chat.id)
        if incoming != OWNER_CHAT_ID:
            # Silently ignore — do NOT acknowledge the message at all
            logger.warning("Blocked request from unauthorized chat_id: %s", incoming)
            return
        return await func(update, context)
    return wrapper


def require_session(func):
    """Layer 2 — reject requests when no active PIN session exists.
    Only enforced when BOT_SECRET_PIN is configured in .env.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if BOT_SECRET_PIN:
            chat_id = str(update.effective_chat.id)
            if not _session_active(chat_id):
                await update.message.reply_text(
                    "🔒 Session not active.\n"
                    "Use /auth followed by your PIN to unlock the bot."
                )
                return
        return await func(update, context)
    return wrapper


def _sanitize_error(err: Exception) -> str:
    """Return a safe, generic error string — never expose paths or keys."""
    msg = str(err)
    # Strip anything that looks like an absolute path
    import re
    msg = re.sub(r"/[^\s'\"]{3,}", "[path]", msg)
    # Strip anything that looks like an API key (long alphanumeric tokens)
    msg = re.sub(r"\b(sk-ant-|sk-)[A-Za-z0-9\-_]{10,}\b", "[key]", msg)
    # Truncate to keep messages short
    return msg[:200]


# ──────────────────────────────────────────────
# Auth commands  (layer-1 only — no session required)
# ──────────────────────────────────────────────

@owner_only
async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /auth <pin> — start a PIN session."""
    chat_id = str(update.effective_chat.id)

    # Always delete the auth message first so the PIN is never visible
    try:
        await update.message.delete()
    except Exception:
        pass   # deletion may fail if already gone

    if not BOT_SECRET_PIN:
        await context.bot.send_message(
            chat_id=chat_id,
            text="ℹ️ No PIN is configured. Auth is not required.",
        )
        return

    provided = " ".join(context.args).strip() if context.args else ""

    if not _auth_allowed(chat_id):
        logger.warning("Auth rate-limit triggered for chat_id %s", chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="⛔ Too many failed attempts. Try again later.",
        )
        return

    if provided == BOT_SECRET_PIN:
        _grant_session(chat_id)
        ttl_h = SESSION_TTL // 3600
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Authenticated. Session valid for {ttl_h} hour(s).\nUse /lock to end it early.",
        )
    else:
        _record_auth_failure(chat_id)
        remaining = _MAX_AUTH_FAILURES - len(_auth_failures.get(chat_id, []))
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Incorrect PIN. {max(remaining, 0)} attempt(s) remaining.",
        )


@owner_only
async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /lock — manually revoke the current session."""
    chat_id = str(update.effective_chat.id)
    _revoke_session(chat_id)
    await update.message.reply_text("🔒 Session locked. Use /auth to re-authenticate.")


# ──────────────────────────────────────────────
# Command Handlers  (both layers enforced)
# ──────────────────────────────────────────────

@owner_only
@require_session
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start and /help commands."""
    pin_note = (
        "\n\n🔒 *PIN auth is active.* Use /auth to unlock, /lock to secure."
        if BOT_SECRET_PIN else ""
    )
    text = (
        "*AI Builder Bot — Commands:*\n\n"
        "/build — Generate a full project\n"
        "/status — View your recent builds\n"
        "/agents — List internal modules\n"
        "/clear — Reset conversation history\n"
        "/lock — Lock the bot session\n"
        "/help — Show this message"
        f"{pin_note}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
@require_session
async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /build — generate a project."""
    chat_id = str(update.effective_chat.id)

    prompt = " ".join(context.args).strip() if context.args else ""
    if not prompt:
        await update.message.reply_text(
            "Usage: /build followed by a project description\n"
            "Example: /build a landing page for a startup"
        )
        return

    # Basic prompt length guard
    if len(prompt) > 1000:
        await update.message.reply_text("Prompt too long (max 1000 characters).")
        return

    await update.message.reply_text("Building your project…  This may take a minute.")

    try:
        result = build_project(prompt)

        memory.log_task(
            chat_id=chat_id,
            prompt=prompt,
            project_name=result["project_name"],
            model=result["model_used"],
            status="completed",
            output_path=result["directory"],
        )

        file_list = "\n".join(f"  • {os.path.basename(f)}" for f in result["files_created"])

        reply = (
            f"*Project Built Successfully!*\n\n"
            f"*Name:* {result['project_name']}\n"
            f"*Description:* {result['description']}\n\n"
            f"*Files created:*\n{file_list}\n\n"
            f"*Output:* `{result['directory']}`\n\n"
            f"*Setup:*\n{result['setup_instructions']}\n\n"
            f"*Model:* {result['model_used']}\n"
            f"*Cost:* ${result['cost_usd']:.4f}"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")

        await update.message.reply_text("Running QA review…")
        review = qa_review(result)
        await update.message.reply_text(f"*QA Review:*\n\n{review}", parse_mode="Markdown")

    except ValueError as e:
        # ValueError from builder is safe to surface (no paths)
        logger.error("Build ValueError: %s", e)
        memory.log_task(chat_id=chat_id, prompt=prompt, project_name="(failed)",
                        model="N/A", status="failed", output_path="")
        await update.message.reply_text(f"Build failed: {e}")

    except Exception as e:
        logger.error("Build error: %s", e, exc_info=True)
        memory.log_task(chat_id=chat_id, prompt=prompt, project_name="(failed)",
                        model="N/A", status="failed", output_path="")
        await update.message.reply_text("Build failed due to an internal error.")


@owner_only
@require_session
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status — show recent builds."""
    chat_id = str(update.effective_chat.id)
    tasks = memory.get_recent_tasks(chat_id, limit=5)

    if not tasks:
        await update.message.reply_text("No builds yet. Try /build to get started!")
        return

    lines = ["*Recent Builds:*\n"]
    for i, task in enumerate(tasks, 1):
        icon = "✅" if task["status"] == "completed" else "❌"
        lines.append(
            f"{i}. {icon} *{task['project']}*\n"
            f"   Model: {task['model']}\n"
            f"   Time: {task['time']}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
@require_session
async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /agents — list internal modules."""
    text = (
        "*Internal Modules:*\n\n"
        "*router.py* — Model selection\n"
        "Routes tasks to Haiku / Sonnet / Opus based on prompt keywords.\n\n"
        "*builder.py* — Project builder\n"
        "Generates project scaffolds with sandboxed file writes and QA review.\n\n"
        "*memory.py* — Persistent memory\n"
        "SQLite-backed conversation history and build task log.\n\n"
        "*media.py* — Media analysis\n"
        "Image and PDF analysis via Claude vision."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
@require_session
async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clear and /reset — clear conversation history."""
    chat_id = str(update.effective_chat.id)
    memory.clear_history(chat_id)
    await update.message.reply_text("Conversation history cleared.")


# ──────────────────────────────────────────────
# Message Handlers  (both layers enforced)
# ──────────────────────────────────────────────

@owner_only
@require_session
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages — chat with Claude."""
    chat_id   = str(update.effective_chat.id)
    user_text = update.message.text

    # Length guard
    if len(user_text) > 4000:
        await update.message.reply_text("Message too long (max 4000 characters).")
        return

    memory.add_message(chat_id, "user", user_text)
    history  = memory.get_history(chat_id, limit=10)
    model_id, _ = route_model(user_text)

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        messages = [{"role": m["role"], "content": m["content"]} for m in history]

        response = client.messages.create(
            model=model_id,
            max_tokens=1500,
            system=SYSTEM_CHAT,
            messages=messages,
        )

        reply_text = response.content[0].text
        memory.add_message(chat_id, "assistant", reply_text)

        model_name = get_model_alias(model_id)
        cost = estimate_cost(response.usage.input_tokens,
                             response.usage.output_tokens, model_id)
        footer = f"\n\n_{model_name} • ${cost:.4f}_"

        await update.message.reply_text(reply_text + footer, parse_mode="Markdown")

    except Exception as e:
        logger.error("Chat error: %s", e, exc_info=True)
        await update.message.reply_text("Something went wrong. Please try again.")


@owner_only
@require_session
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages — Claude vision analysis."""
    await update.message.reply_text("Analyzing your image…")

    try:
        photo      = update.message.photo[-1]
        file       = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        question   = update.message.caption

        result = analyze_image(bytes(photo_bytes), "image/jpeg", question)
        await update.message.reply_text(result)

    except Exception as e:
        logger.error("Photo analysis error: %s", e, exc_info=True)
        await update.message.reply_text("Image analysis failed. Please try again.")


@owner_only
@require_session
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document messages — PDFs and images."""
    doc       = update.message.document
    mime_type = doc.mime_type or ""

    if mime_type == "application/pdf":
        await update.message.reply_text("Analyzing your PDF…  This may take a moment.")
        try:
            file      = await context.bot.get_file(doc.file_id)
            doc_bytes = await file.download_as_bytearray()
            result    = analyze_pdf(bytes(doc_bytes), update.message.caption)

            if len(result) > 4000:
                for chunk in (result[i:i+4000] for i in range(0, len(result), 4000)):
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(result)

        except Exception as e:
            logger.error("PDF analysis error: %s", e, exc_info=True)
            await update.message.reply_text("PDF analysis failed. Please try again.")

    elif mime_type.startswith("image/"):
        await update.message.reply_text("Analyzing your image…")
        try:
            file      = await context.bot.get_file(doc.file_id)
            doc_bytes = await file.download_as_bytearray()
            result    = analyze_image(bytes(doc_bytes), mime_type, update.message.caption)
            await update.message.reply_text(result)

        except Exception as e:
            logger.error("Image analysis error: %s", e, exc_info=True)
            await update.message.reply_text("Image analysis failed. Please try again.")

    else:
        await update.message.reply_text(
            "Only *PDFs* and *images* are supported.\n"
            "Supported: PDF, JPG, PNG, GIF, WEBP",
            parse_mode="Markdown",
        )


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    """Start the bot with pre-flight security checks."""

    # Critical config checks — refuse to start if missing
    missing = []
    if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not ANTHROPIC_API_KEY:  missing.append("ANTHROPIC_API_KEY")
    if not OWNER_CHAT_ID:      missing.append("TELEGRAM_CHAT_ID")
    if missing:
        for var in missing:
            print(f"ERROR: {var} is not set in .env — bot cannot start")
        return

    if not BOT_SECRET_PIN:
        logger.warning(
            "BOT_SECRET_PIN is not set. Anyone who gains access to your Telegram "
            "account can use this bot. Set BOT_SECRET_PIN in .env for full protection."
        )
    else:
        logger.info("PIN authentication is ENABLED (session TTL: %dh)", SESSION_TTL // 3600)

    logger.info("Owner chat_id: %s", OWNER_CHAT_ID)

    memory.init_db()
    logger.info("Database initialised")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Auth commands (layer-1 only)
    app.add_handler(CommandHandler("auth",  cmd_auth))
    app.add_handler(CommandHandler("lock",  cmd_lock))

    # Standard commands (both layers)
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("build",  cmd_build))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CommandHandler("reset",  cmd_clear))

    # Message handlers (both layers)
    app.add_handler(MessageHandler(filters.PHOTO,         handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL,  handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running — only responding to chat_id %s", OWNER_CHAT_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
