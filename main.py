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


async def generate_exam_ca_quiz(category: str) -> dict:
    """Groq Llama-3.1-8b-instant se MCQ quiz generate karna."""
    prompt = f"""
    Create 1 high-yield, factual Multiple Choice Question (MCQ) for competitive exams (UPSC/SSC/State PCS/Banking).
    Target Topic/Domain: {category}.
    Focus: Recent events, government schemes, summits, military exercises, indices, appointments, economy, or sports.
    Language: Bilingual/Hinglish.

    Rules:
    - 4 realistic and distinct options.
    - Provide an informative 1-2 line factual explanation.
    - Return RAW JSON ONLY matching the schema.

    JSON Schema:
    {{
      "question": "Question statement here",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct_option_id": 0,
      "explanation": "Key factual detail for exam revision"
    }}
    """

    chat_completion = await groq_client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.1-8b-instant",
        temperature=0.2,
        response_format={"type": "json_object"}
    )

    content = chat_completion.choices[0].message.content.strip()
    return json.loads(content)


# ==================== COMMAND HANDLERS ====================

@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    print(f"📥 /start received from user: {message.from_user.id}")
    text = (
        "👋 **Namaste! Main Competition Current Affairs Quiz Bot hoon.**\n\n"
        "🎯 Yahan UPSC, SSC, Banking aur State Exams ke liye daily latest current affairs aur GS practice kar sakte hain.\n\n"
        "⚙️ Commands dekhne ke liye **/help** dabayein."
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
    print(f"📥 /help received from user: {message.from_user.id}")
    help_text = (
        "📚 **Bot Command Guide:**\n\n"
        "🔹 `/ca` - Latest General Current Affairs question send karega.\n"
        "🔹 `/ca <topic>` - Specific topic par question generate karega.\n"
        "   _Example: `/ca Schemes`, `/ca Defence`, `/ca Sports`_\n"
        "🔹 `/settings` - Quiz Timer settings badalne ke liye.\n"
        "🔹 `/setgroup` - Is group ko authorized list me lock karein (Owner only).\n"
        "🔹 `/id` - Chat ID aur User ID dekhne ke liye.\n"
        "🔹 `/help` - Is help menu ko dekhne ke liye.\n\n"
        "⚠️ _Note: Quiz start karne aur settings badalne ki permission sirf Owner ke paas hai._"
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
    print(f"📥 /settings received from user: {message.from_user.id}")
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Yeh command sirf Owner use kar sakta hai!**")
        return

    text = f"⚙️ **Quiz Settings:**\n\n⏱️ **Current Timer:** `{TIMER_SECONDS}` Seconds\nNaya timer chunein:"
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
    print(f"📥 /setgroup in chat: {message.chat.id} by: {message.from_user.id}")
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
    print(f"📥 /ca received in Chat: {message.chat.id} from User: {message.from_user.id}")

    # 1. Allowed Group Restriction
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        print(f"🚫 Chat {message.chat.id} not in ALLOWED_CHAT_IDS: {ALLOWED_CHAT_IDS}")
        return

    # 2. Strict Owner Check
    owner_check = await is_owner(client, message.chat.id, message.from_user.id)
    print(f"👤 User {message.from_user.id} Owner Status: {owner_check}")

    if not owner_check:
        await message.reply_text("⛔ **Yeh command sirf Group Owner ke liye reserved hai!**")
        return

    category = "Government Schemes, Summits, Defense and Economy"
    if len(message.command) > 1:
        category = " ".join(message.command[1:])

    status_msg = await message.reply_text(f"🎯 **[Owner Initiated]** `{category}` par question taiyar ho raha hai...")

    try:
        data = await generate_exam_ca_quiz(category)
        options = data["options"][:4]

        await client.send_poll(
            chat_id=message.chat.id,
            question=f"⏱ [Timer: {TIMER_SECONDS}s]\n" + data["question"],
            options=options,
            is_anonymous=False,
            type="quiz",
            correct_option_id=int(data["correct_option_id"]),
            explanation=data.get("explanation", ""),
            open_period=TIMER_SECONDS
        )
        await status_msg.delete()

    except Exception as e:
        print(f"❌ Error generating/sending quiz: {e}")
        await status_msg.edit_text(f"❌ Error: Question generate nahi ho saka.\n`{e}`")


# ==================== CALLBACK BUTTON HANDLERS ====================

@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    global TIMER_SECONDS
    data = query.data

    if data == "btn_help":
        await query.answer()
        await query.message.reply_text("🔹 `/ca` - Latest CA Question\n🔹 `/settings` - Timer Badalne Ke Liye")

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


# ==================== WEB SERVER (RENDER DUMMY PORT) ====================

async def handle_ping(request):
    return web.Response(text="Bot is running active!")

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = web.Application()
    server.router.add_get("/", handle_ping)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Web server listening on port {port} (Render Port Bind Successful)")


# ==================== BOT RUNNER ====================

async def main():
    await start_web_server()

    print("Owner CA Quiz Bot starting Pyrogram...")
    await app.start()

    # Commands list set karna
    try:
        commands = [
            BotCommand("ca", "Start Current Affairs Quiz (Owner Only)"),
            BotCommand("settings", "Configure Quiz Timer & Settings"),
            BotCommand("setgroup", "Authorize this group for quizzes"),
            BotCommand("id", "Get Group and User IDs"),
            BotCommand("help", "Show help and command guide"),
            BotCommand("start", "Start the bot interface")
        ]
        await app.set_bot_commands(commands)
        print("✅ Telegram Command Menu Suggestions successfully set!")
    except Exception as e:
        print(f"⚠️ Command menu set warning: {e}")

    await idle()
    await app.stop()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
