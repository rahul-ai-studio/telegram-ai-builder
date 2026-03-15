"""Telegram AI Builder Bot — Main entry point.

Send /build commands to generate full projects, chat with Claude,
analyze images and PDFs, all from Telegram.
"""

from __future__ import annotations

import logging
import os

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

# Load environment variables
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Command Handlers
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start and /help commands."""
    chat_id = update.effective_chat.id
    text = (
        f"*Welcome to AI Builder Bot!*\n\n"
        f"Your Chat ID: `{chat_id}`\n\n"
        f"*Commands:*\n"
        f"/build — Generate a full project from a description\n"
        f"/status — View your recent builds\n"
        f"/agents — List internal modules\n"
        f"/clear — Reset conversation history\n"
        f"/help — Show this message\n\n"
        f"You can also:\n"
        f"• Send any text message to chat with Claude\n"
        f"• Send a photo for AI image analysis\n"
        f"• Send a PDF for AI summarization"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /build command to generate a project."""
    chat_id = str(update.effective_chat.id)

    # Get the prompt after /build
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text(
            "Usage: /build followed by a project description\n\n"
            "Example: /build a landing page for a startup idea"
        )
        return

    await update.message.reply_text(
        f"Building your project... This may take a minute."
    )

    try:
        result = build_project(prompt)

        # Log the task
        memory.log_task(
            chat_id=chat_id,
            prompt=prompt,
            project_name=result["project_name"],
            model=result["model_used"],
            status="completed",
            output_path=result["directory"],
        )

        # Format the file list
        file_list = "\n".join(
            f"  • {os.path.basename(f)}" for f in result["files_created"]
        )

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

        # Run QA review
        await update.message.reply_text("Running QA review...")
        review = qa_review(result)
        await update.message.reply_text(
            f"*QA Review:*\n\n{review}", parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Build failed: {e}")
        memory.log_task(
            chat_id=chat_id,
            prompt=prompt,
            project_name="(failed)",
            model="N/A",
            status="failed",
            output_path="",
        )
        await update.message.reply_text(f"Build failed: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command to show recent builds."""
    chat_id = str(update.effective_chat.id)
    tasks = memory.get_recent_tasks(chat_id, limit=5)

    if not tasks:
        await update.message.reply_text("No builds yet. Try /build to get started!")
        return

    lines = ["*Recent Builds:*\n"]
    for i, task in enumerate(tasks, 1):
        status_icon = "✅" if task["status"] == "completed" else "❌"
        lines.append(
            f"{i}. {status_icon} *{task['project']}*\n"
            f"   Model: {task['model']}\n"
            f"   Time: {task['time']}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /agents command to list internal modules."""
    text = (
        "*Internal Modules:*\n\n"
        "*router.py* — Model selection\n"
        "Routes tasks to Haiku (simple), Sonnet (standard), or Opus (complex) "
        "based on prompt keywords. Estimates API costs.\n\n"
        "*builder.py* — Project builder\n"
        "Generates complete project scaffolds with code files, README, and "
        "setup instructions. Runs QA reviews on output.\n\n"
        "*memory.py* — Persistent memory\n"
        "SQLite-backed storage for conversation history, build tasks, and "
        "code snippets. Survives restarts.\n\n"
        "*media.py* — Media analysis\n"
        "Analyzes images using Claude's vision and summarizes PDFs by "
        "converting pages to images."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clear and /reset commands to clear conversation history."""
    chat_id = str(update.effective_chat.id)
    memory.clear_history(chat_id)
    await update.message.reply_text("Conversation history cleared.")


# ──────────────────────────────────────────────
# Message Handlers
# ──────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages — chat with Claude."""
    chat_id = str(update.effective_chat.id)
    user_text = update.message.text

    # Save user message
    memory.add_message(chat_id, "user", user_text)

    # Get conversation history
    history = memory.get_history(chat_id, limit=10)

    # Route to the right model
    model_id, reason = route_model(user_text)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # Build messages from history
        messages = [{"role": m["role"], "content": m["content"]} for m in history]

        response = client.messages.create(
            model=model_id,
            max_tokens=1500,
            system=SYSTEM_CHAT,
            messages=messages,
        )

        reply_text = response.content[0].text

        # Save assistant reply
        memory.add_message(chat_id, "assistant", reply_text)

        # Add model info footer
        model_name = get_model_alias(model_id)
        cost = estimate_cost(
            response.usage.input_tokens,
            response.usage.output_tokens,
            model_id,
        )
        footer = f"\n\n_{model_name} • ${cost:.4f}_"

        await update.message.reply_text(
            reply_text + footer, parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Chat error: {e}")
        await update.message.reply_text(f"Sorry, something went wrong: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages — analyze with Claude vision."""
    await update.message.reply_text("Analyzing your image...")

    try:
        # Get the highest resolution photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        # Download the photo
        photo_bytes = await file.download_as_bytearray()

        # Get caption as question if provided
        question = update.message.caption

        # Analyze the image
        result = analyze_image(
            image_bytes=bytes(photo_bytes),
            mime_type="image/jpeg",
            question=question,
        )

        await update.message.reply_text(result)

    except Exception as e:
        logger.error(f"Photo analysis error: {e}")
        await update.message.reply_text(f"Image analysis failed: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document messages — analyze PDFs and images."""
    doc = update.message.document
    mime_type = doc.mime_type or ""

    if mime_type == "application/pdf":
        await update.message.reply_text("Analyzing your PDF... This may take a moment.")

        try:
            file = await context.bot.get_file(doc.file_id)
            doc_bytes = await file.download_as_bytearray()

            question = update.message.caption
            result = analyze_pdf(
                pdf_bytes=bytes(doc_bytes),
                question=question,
            )

            # Split long messages (Telegram limit is 4096 chars)
            if len(result) > 4000:
                parts = [result[i:i + 4000] for i in range(0, len(result), 4000)]
                for part in parts:
                    await update.message.reply_text(part)
            else:
                await update.message.reply_text(result)

        except Exception as e:
            logger.error(f"PDF analysis error: {e}")
            await update.message.reply_text(f"PDF analysis failed: {e}")

    elif mime_type.startswith("image/"):
        await update.message.reply_text("Analyzing your image...")

        try:
            file = await context.bot.get_file(doc.file_id)
            doc_bytes = await file.download_as_bytearray()

            question = update.message.caption
            result = analyze_image(
                image_bytes=bytes(doc_bytes),
                mime_type=mime_type,
                question=question,
            )

            await update.message.reply_text(result)

        except Exception as e:
            logger.error(f"Image analysis error: {e}")
            await update.message.reply_text(f"Image analysis failed: {e}")

    else:
        await update.message.reply_text(
            "Sorry, I can only analyze *PDFs* and *images* right now.\n"
            "Supported formats: PDF, JPG, PNG, GIF, WEBP",
            parse_mode="Markdown",
        )


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        return

    # Initialize database
    memory.init_db()
    logger.info("Database initialized")

    # Build application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("reset", cmd_clear))

    # Register message handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
