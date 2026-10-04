import os
import json
import asyncio
from aiohttp import web
from groq import AsyncGroq
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import (
    Message,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from dotenv import load_dotenv

load_dotenv()

# --- Config Variables ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

TIMER_SECONDS = int(os.getenv("TIMER_SECONDS", "30"))

raw_chats = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [int(cid.strip()) for cid in raw_chats.split(",") if cid.strip()]

app = Client(
    "competition_ca_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)


async def is_owner(client: Client, chat_id: int, user_id: int) -> bool:
    """Check owner status (Global Bot Owner or Group Creator)."""
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status == ChatMemberStatus.OWNER
    except Exception as e:
        print(f"⚠️ Error checking owner status: {e}")
        return False


async def get_active_groq_model() -> str:
    """Groq API se live active models ki list check karke functional model select karna."""
    preferred_models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "gemma2-9b-it"
    ]
    try:
        models_data = await groq_client.models.list()
        active_ids = [m.id for m in models_data.data if getattr(m, "active", True)]
        for pref in preferred_models:
            if pref in active_ids:
                return pref
        if active_ids:
            return active_ids[0]
    except Exception as e:
        print(f"⚠️ Groq live models fetch warning: {e}")
    return "llama-3.3-70b-versatile"


async def generate_universal_quiz(user_topic: str) -> dict:
    """UPSC, SSC, BPSC, Banking, State PCS ya kisi bhi syllabus ka factual MCQ generate karna."""
    prompt = f"""
    You are an expert exam setter for Indian competitive exams (UPSC, SSC CGL/CHSL, BPSC, State PCS, Banking, Railway, etc.).

    Target Topic / Subject / Exam: "{user_topic}"

    Instructions:
    1. Create exactly 1 high-quality, authentic Multiple Choice Question (MCQ) strictly matching the requested topic/exam syllabus.
    2. If the user mentions a specific exam (e.g. BPSC, UPSC, SSC), strictly match that exam's difficulty and standard.
    3. If the topic is static (History, Polity, Geography, Science, Math, Reasoning, Bihar Special), frame a concept-based or factually accurate question.
    4. If the topic is Current Affairs, focus on real verified developments, schemes, indices, or appointments.
    5. Language: Bilingual/Hinglish (Hindi + English key terms) for easy comprehension by aspirants.
    6. Provide 4 distinct options and 1 concise factual explanation.
    7. Return RAW JSON ONLY without any markdown backticks.

    Required JSON Schema:
    {{
      "question": "Question text here (Bilingual/Hinglish)",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct_option_id": 0,
      "explanation": "Clear 1-2 line explanation highlighting the core fact"
    }}
    """

    model_name = await get_active_groq_model()

    chat_completion = await groq_client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=model_name,
        temperature=0.3,
        response_format={"type": "json_object"}
    )

    content = chat_completion.choices[0].message.content.strip()
    return json.loads(content)


# ==================== DUMMY WEB SERVER (RENDER PORT BIND FIX) ====================

async def handle_ping(request):
    return web.Response(text="Bot is running active 24/7!")

async def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = web.Application()
    server.router.add_get("/", handle_ping)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Dummy web server listening on port {port} (Render Port Scan Resolved)")


# ==================== COMMAND HANDLERS ====================

@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    text = (
        "👋 **Namaste! Main Universal Competition Quiz Bot hoon.**\n\n"
        "🎯 Aap kisi bhi exam ya subject ka quiz run kar sakte hain:\n"
        "• **UPSC / State PCS / BPSC** (History, Polity, Bihar Special, Economy)\n"
        "• **SSC CGL / CHSL** (GK, GS, Science, Reasoning, Math)\n"
        "• **Railway / Banking / Defence**\n"
        "• **Daily Current Affairs**\n\n"
        "📖 Commands sikhne ke liye **/help** dabayein."
    )
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Help Guide", callback_data="btn_help"),
            InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")
        ]
    ])
    await message.reply_text(text, reply_markup=buttons)


@app.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message):
    help_text = (
        "📚 **Quiz Command Guide (Kisi bhi syllabus par question banayein):**\n\n"
        "🔹 `/ca` - Mixed GS & Latest Current Affairs\n"
        "🔹 `/ca <exam ya topic>` - Specific exam ya syllabus ka question:\n"
        "   • `/ca bpsc bihar special history`\n"
        "   • `/ca upsc polity preamble`\n"
        "   • `/ca ssc cgl ancient history`\n"
        "   • `/ca railway general science chemistry`\n"
        "   • `/ca economics banking repo rate`\n"
        "   • `/ca sports current affairs`\n\n"
        "🔹 `/settings` - Quiz Timer customize karein (15s, 30s, 45s, 60s)\n"
        "🔹 `/setgroup` - Group ko authorize karein (Owner only)\n"
        "🔹 `/id` - Chat ID aur User ID check karein"
    )
    await message.reply_text(help_text)


@app.on_message(filters.command("id"))
async def id_handler(client: Client, message: Message):
    await message.reply_text(
        f"📌 **Chat ID:** `{message.chat.id}`\n"
        f"👤 **User ID:** `{message.from_user.id}`"
    )


@app.on_message(filters.command("settings"))
async def settings_handler(client: Client, message: Message):
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Yeh command sirf Owner use kar sakta hai!**")
        return

    text = f"⚙️ **Quiz Settings:**\n\n⏱️️ **Current Timer:** `{TIMER_SECONDS}` Seconds\nNaya timer chunein:"
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("15s", callback_data="set_time_15"),
            InlineKeyboardButton("30s", callback_data="set_time_30"),
            InlineKeyboardButton("45s", callback_data="set_time_45"),
            InlineKeyboardButton("60s", callback_data="set_time_60")
        ]
    ])
    await message.reply_text(text, reply_markup=buttons)


@app.on_message(filters.command("setgroup") & filters.group)
async def setgroup_handler(client: Client, message: Message):
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Sirf Owner hi group set kar sakta hai!**")
        return

    if message.chat.id not in ALLOWED_CHAT_IDS:
        ALLOWED_CHAT_IDS.append(message.chat.id)

    await message.reply_text(
        f"✅ **Group Authorized Successfully!**\n\n"
        f"📌 **Group:** {message.chat.title}\n"
        f"🆔 **Chat ID:** `{message.chat.id}`"
    )


@app.on_message(filters.command("ca"))
async def exam_quiz_handler(client: Client, message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        return

    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Yeh command sirf Group Owner ke liye reserved hai!**")
        return

    if len(message.command) > 1:
        topic = " ".join(message.command[1:])
    else:
        topic = "Mix GS and Recent Current Affairs for UPSC/SSC/BPSC"

    status_msg = await message.reply_text(f"🎯 **[Quiz Generator]** `{topic}` par question taiyar ho raha hai...")

    try:
        data = await generate_universal_quiz(topic)
        options = data["options"][:4]

        await client.send_poll(
            chat_id=message.chat.id,
            question=f"⏱ [{TIMER_SECONDS}s] Topic: {topic}\n\n" + data["question"],
            options=options,
            is_anonymous=False,
            type="quiz",
            correct_option_id=int(data["correct_option_id"]),
            explanation=data.get("explanation", ""),
            open_period=TIMER_SECONDS
        )
        await status_msg.delete()

    except Exception as e:
        print(f"❌ Error generating quiz: {e}")
        await status_msg.edit_text(f"❌ Error: Question generate nahi ho saka.\n`{e}`")


# ==================== CALLBACK BUTTON HANDLERS ====================

@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    global TIMER_SECONDS
    data = query.data

    if data == "btn_help":
        await query.answer()
        help_text = (
            "🔹 `/ca <topic>` - Kisi bhi subject/exam par question banayein\n"
            "🔹 `/settings` - Quiz Timer badalein\n"
            "🔹 `/id` - Chat ID check karein"
        )
        await query.message.reply_text(help_text)

    elif data == "btn_settings":
        await query.answer()
        if not await is_owner(client, query.message.chat.id, query.from_user.id):
            await query.answer("⛔ Sirf Owner settings badal sakta hai!", show_alert=True)
            return

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("15s", callback_data="set_time_15"),
                InlineKeyboardButton("30s", callback_data="set_time_30"),
                InlineKeyboardButton("45s", callback_data="set_time_45"),
                InlineKeyboardButton("60s", callback_data="set_time_60")
            ]
        ])
        await query.message.reply_text("⏱️ Timer select karein:", reply_markup=buttons)

    elif data.startswith("set_time_"):
        if not await is_owner(client, query.message.chat.id, query.from_user.id):
            await query.answer("⛔ Sirf Owner timer change kar sakta hai!", show_alert=True)
            return

        new_time = int(data.split("_")[2])
        TIMER_SECONDS = new_time
        await query.answer(f"✅ Timer {new_time}s par set ho gaya!", show_alert=True)
        await query.message.edit_text(f"✅ **Timer updated:** `{TIMER_SECONDS}` Seconds")


# ==================== STARTUP NOTIFICATIONS & RUNNER ====================

async def send_startup_alert():
    """Bot live hote hi Admin/Group me notification send karta hai."""
    target_id = LOG_GROUP_ID if LOG_GROUP_ID else OWNER_ID
    if not target_id:
        print("⚠️ LOG_GROUP_ID aur OWNER_ID dono set nahi hain. Startup alert skip kiya gaya.")
        return

    try:
        bot_info = await app.get_me()
        alert_text = (
            "🚀 **Universal Quiz Bot Started Successfully!**\n\n"
            f"🤖 **Bot:** @{bot_info.username}\n"
            f"⏱️ **Default Timer:** `{TIMER_SECONDS}s`\n"
            f"🎯 **Support:** UPSC, SSC, BPSC, State PCS & All Subjects\n"
            f"⚡ **Status:** Active & Ready for Quiz!"
        )
        await app.send_message(chat_id=target_id, text=alert_text)
        print(f"✅ Startup alert successfully sent to target ID: {target_id}")
    except Exception as e:
        print(f"❌ Startup alert send karne me error aaya (Target ID: {target_id}): {e}")


async def set_menu_suggestions():
    """Telegram menu bar suggestions configure karna."""
    await asyncio.sleep(2)
    try:
        commands = [
            BotCommand("ca", "Run Quiz on any exam/syllabus topic"),
            BotCommand("settings", "Configure Quiz Timer & Settings"),
            BotCommand("setgroup", "Authorize this group for quizzes"),
            BotCommand("id", "Get Group and User IDs"),
            BotCommand("help", "Show help and command guide"),
            BotCommand("start", "Start the bot interface")
        ]
        await app.set_bot_commands(commands)
        print("✅ Telegram Command Menu Suggestions set successfully!")
    except Exception as e:
        print(f"⚠️ Suggestions set warning: {e}")


async def main():
    # 1. Render dummy web server start
    await start_dummy_server()

    # 2. Pyrogram bot connect
    print("🚀 Connecting Pyrogram Client to Telegram...")
    await app.start()
    print("✅ Pyrogram Client Connected & Listening for Messages!")

    # 3. Direct await startup alert
    await send_startup_alert()

    # 4. Suggestions set karna background task me
    asyncio.create_task(set_menu_suggestions())

    # 5. Event loop ko idle me rakhna
    await idle()
    await app.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        pass
