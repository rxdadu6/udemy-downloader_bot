#!/usr/bin/env python3
import os
import re
import sys
import json
import time
import shutil
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime
from threading import Thread
from queue import Queue, Empty

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("7933914808:AAGDTVuwJmoyioCuU7HzI30PQ51DEXU49sY")
if not BOT_TOKEN:
    print("❌ TELEGRAM_BOT_TOKEN not set in .env or environment")
    sys.exit(1)

# Conversation states
TOKEN, URL, OPTIONS = range(3)

# Global queue for progress messages
progress_queue = Queue()

class Downloader:
    def __init__(self, course_url, bearer, chat_id, quality="720", captions=False, skip_hls=False):
        self.course_url = course_url
        self.bearer = bearer
        self.chat_id = chat_id
        self.quality = quality
        self.captions = captions
        self.skip_hls = skip_hls
        self.output_dir = Path(f"./course_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        self.running = False

    def run(self, update_callback):
        """Run the downloader and upload videos."""
        self.running = True
        try:
            # Build command
            cmd = [
                "python", "main.py",
                "-c", self.course_url,
                "-o", str(self.output_dir),
                "-q", self.quality,
                "-b", self.bearer,
            ]
            if self.captions:
                cmd.append("--download-captions")
            if self.skip_hls:
                cmd.append("--skip-hls")

            update_callback("📥 Starting download...")
            update_callback(f"Command: {' '.join(cmd)}")

            # Run downloader
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )

            # Read output and send progress
            for line in iter(process.stdout.readline, ''):
                if line:
                    update_callback(f"📄 {line.strip()}")
            process.wait()

            if process.returncode != 0:
                update_callback(f"❌ Downloader failed with code {process.returncode}")
                self.running = False
                return

            update_callback("✅ Download completed. Looking for video files...")

            # Find videos
            video_exts = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm"}
            videos = sorted([p for p in self.output_dir.rglob("*") if p.is_file() and p.suffix.lower() in video_exts])

            if not videos:
                update_callback("⚠️ No video files found.")
                self.running = False
                return

            update_callback(f"📤 Found {len(videos)} video(s). Uploading to Telegram...")

            # Upload each video
            for idx, v in enumerate(videos, 1):
                update_callback(f"⬆️ [{idx}/{len(videos)}] Uploading {v.name} ({v.stat().st_size / (1024*1024):.1f} MB)")
                success, msg_id = self.upload_video(v)
                if success:
                    link = self.get_message_link(msg_id)
                    update_callback(f"✅ Uploaded: {v.name}\n🔗 Link: {link}")
                    # Delete local file
                    try:
                        v.unlink()
                        update_callback(f"🗑️ Deleted local file: {v}")
                    except Exception as e:
                        update_callback(f"⚠️ Could not delete {v}: {e}")
                else:
                    update_callback(f"❌ Upload failed for {v.name}, keeping file.")
                time.sleep(0.5)  # rate limit

            update_callback("🎉 All videos processed!")
        except Exception as e:
            update_callback(f"💥 Unexpected error: {e}")
        finally:
            self.running = False

    def upload_video(self, file_path):
        """Upload video using Telegram Bot API."""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        with open(file_path, "rb") as f:
            files = {"document": f}
            data = {"chat_id": self.chat_id, "caption": file_path.name}
            try:
                import requests
                resp = requests.post(url, data=data, files=files, timeout=600)
                if resp.ok:
                    msg_id = resp.json()["result"]["message_id"]
                    return True, msg_id
                else:
                    return False, None
            except Exception as e:
                return False, None

    def get_message_link(self, msg_id):
        """Generate t.me link for the message."""
        chat_id = str(self.chat_id)
        # For supergroups, chat_id is like -1001234567890
        if chat_id.startswith("-100"):
            chat_id = chat_id[4:]
        elif chat_id.startswith("-"):
            chat_id = chat_id[1:]
        return f"https://t.me/c/{chat_id}/{msg_id}"

# ---------- Bot Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message and start conversation."""
    await update.message.reply_text(
        "👋 Welcome to the Udemy Downloader Bot!\n\n"
        "I'll help you download any Udemy course and send the videos directly to this chat.\n\n"
        "⚠️ **You'll need:**\n"
        "• Your Udemy **bearer token** (how to get: send /token_help)\n"
        "• The **course URL**\n\n"
        "Let's start. Please send me your **bearer token** (it looks like a long string of letters/numbers)."
    )
    return TOKEN

async def token_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send instructions for getting bearer token."""
    help_text = (
        "🔑 **How to get your Udemy bearer token:**\n\n"
        "1. Open Udemy in your browser and log in.\n"
        "2. Open Developer Tools (F12) → Network tab.\n"
        "3. Reload the page and look for any request to `udemy.com`.\n"
        "4. In the request headers, find `Authorization: Bearer <token>`.\n"
        "5. Copy that token (it's a long string).\n\n"
        "You can also find it in the cookies under `access_token`.\n\n"
        "Once you have it, send it here."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store bearer token and ask for course URL."""
    token = update.message.text.strip()
    if len(token) < 20:  # simple validation
        await update.message.reply_text("⚠️ That doesn't look like a valid token. Please send a longer string (usually > 50 chars).")
        return TOKEN
    context.user_data["bearer_token"] = token
    await update.message.reply_text(
        "✅ Token saved.\n\n"
        "Now send me the **full Udemy course URL**.\n"
        "Example: `https://www.udemy.com/course/python-for-beginners/`"
    )
    return URL

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store course URL and ask for optional settings."""
    url = update.message.text.strip()
    # Simple URL validation
    if not re.match(r'https?://www\.udemy\.com/course/[\w-]+/?', url):
        await update.message.reply_text("⚠️ That doesn't look like a valid Udemy course URL. Please try again.")
        return URL
    context.user_data["course_url"] = url
    # Ask for options
    keyboard = [
        [
            InlineKeyboardButton("720p (default)", callback_data="q_720"),
            InlineKeyboardButton("1080p", callback_data="q_1080"),
        ],
        [
            InlineKeyboardButton("With captions", callback_data="cap_on"),
            InlineKeyboardButton("No captions", callback_data="cap_off"),
        ],
        [
            InlineKeyboardButton("Skip HLS (for 1080p)", callback_data="skip_on"),
            InlineKeyboardButton("Don't skip HLS", callback_data="skip_off"),
        ],
        [InlineKeyboardButton("✅ Start Download", callback_data="start_dl")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Select your preferences below, then click **Start Download**:\n"
        "(you can also just click Start Download to use defaults)",
        reply_markup=reply_markup
    )
    return OPTIONS

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle option selections and start download."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "start_dl":
        # Gather all options
        bearer = context.user_data.get("bearer_token")
        url = context.user_data.get("course_url")
        if not bearer or not url:
            await query.edit_message_text("⚠️ Missing token or URL. Please start over with /start")
            return ConversationHandler.END

        quality = context.user_data.get("quality", "720")
        captions = context.user_data.get("captions", False)
        skip_hls = context.user_data.get("skip_hls", False)

        await query.edit_message_text(
            f"🔽 **Starting download with settings:**\n"
            f"• Quality: {quality}\n"
            f"• Captions: {'Yes' if captions else 'No'}\n"
            f"• Skip HLS: {'Yes' if skip_hls else 'No'}\n\n"
            f"⏳ This may take a while. I'll send progress updates here."
        )

        # Start download in background thread
        chat_id = update.effective_chat.id
        downloader = Downloader(url, bearer, chat_id, quality, captions, skip_hls)

        def update_callback(msg):
            # Send message via bot (use asyncio.run_coroutine_threadsafe)
            asyncio.run_coroutine_threadsafe(
                context.bot.send_message(chat_id=chat_id, text=msg),
                context.application.loop
            )

        def run_download():
            downloader.run(update_callback)

        thread = Thread(target=run_download, daemon=True)
        thread.start()

        return ConversationHandler.END

    elif data.startswith("q_"):
        quality = data.split("_")[1]
        context.user_data["quality"] = quality
        await query.edit_message_text(f"✅ Quality set to {quality}p")
        # Keep the same keyboard to allow further changes
        return OPTIONS

    elif data.startswith("cap_"):
        captions = data.split("_")[1] == "on"
        context.user_data["captions"] = captions
        await query.edit_message_text(f"✅ Captions: {'Enabled' if captions else 'Disabled'}")
        return OPTIONS

    elif data.startswith("skip_"):
        skip = data.split("_")[1] == "on"
        context.user_data["skip_hls"] = skip
        await query.edit_message_text(f"✅ Skip HLS: {'Enabled' if skip else 'Disabled'}")
        return OPTIONS

    else:
        await query.edit_message_text("Unknown option. Please use the buttons.")
        return OPTIONS

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operation cancelled. Use /start to begin again.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")

# ---------- Main ----------

def main():
    """Run the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token),
                CommandHandler("token_help", token_help),
            ],
            URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url),
            ],
            OPTIONS: [
                CallbackQueryHandler(button_callback),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    print("🤖 Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()