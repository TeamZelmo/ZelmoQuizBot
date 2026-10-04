import os
import json
import asyncio
from g4f.client import AsyncClient
from pyrogram import Client, filters
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

TIMER_SECONDS = int(os.getenv("TIMER_SECONDS", "30"))

raw_chats = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [int(cid.strip()) for cid in raw_chats.split(",") if cid.strip()]

app = Client(
    "competition_ca_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

ai_client = AsyncClient()


async def is_owner(client: Client, chat_id: int, user_id: int) -> bool:
    """Check owner status (Global Bot Owner or Group Creator)."""
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status == ChatMemberStatus.OWNER
    except Exception:
        return False


async def generate_exam_ca_quiz(category: str) -> dict:
    """Generate competitive exam MCQ using G4F with web search."""
    prompt = f"""
    Create 1 high-yield, factual Multiple Choice Question (MCQ) for competitive exams (UPSC/SSC/State PCS/Banking).
    Target Topic/Domain: {category}.
    Focus: Recent events, government schemes, summits, military exercises, indices, appointments, or sports.
    Language: Bilingual/Hinglish.

    Rules:
    - 4 realistic and distinct options.
    - Provide an informative 1-2 line explanation.
    - Return RAW JSON ONLY. No markdown backticks, no text before or after the JSON.

    Schema:
    {{
      "question": "Question statement here",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct_option_id": 0,
      "explanation": "Key factual detail for exam revision"
    }}
    """

    response = await ai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        web_search=True
    )

    content = response.choices[0].message.content.strip()
    clean_text = content.replace("```json", "").replace("```", "").strip()

    start_idx = clean_text.find("{")
    end_idx = clean_text.rfind("}") + 1
    if start_idx != -1 and end_idx != -1:
        clean_text = clean_text[start_idx:end_idx]

    return json.loads(clean_text)


# ==================== COMMAND HANDLERS ====================

@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    text = (
        "👋 **Namaste! Main Competition Current Affairs Quiz Bot hoon.**\n\n"
        "🎯 Yahan UPSC, SSC, Banking aur State Exams ke liye daily latest current affairs aur GS practice kar sakte hain.\n\n"
        "⚙️ Commands dekhne ke liye **/help** dabayein ya neeche button par click karein."
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
        "📚 **Bot Command Guide:**\n\n"
        "🔹 `/ca` - Latest General Current Affairs question send karega.\n"
        "🔹 `/ca <topic>` - Specific topic par question generate karega.\n"
        "   _Example: `/ca Schemes`, `/ca Defence`, `/ca Sports`_\n"
        "🔹 `/settings` - Quiz Timer settings badalne ke liye.\n"
        "🔹 `/setgroup` - Is group ko authorized group list me set karein (Owner only).\n"
        "🔹 `/help` - Is help menu ko dekhne ke liye.\n\n"
        "⚠️ _Note: Quiz trigger karne aur settings badalne ki permission sirf Owner ke paas hai._"
    )
    await message.reply_text(help_text)


@app.on_message(filters.command("settings"))
async def settings_handler(client: Client, message: Message):
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Yeh command sirf Owner use kar sakta hai!**")
        return

    text = (
        f"⚙️ **Quiz Settings:**\n\n"
        f"⏱️ **Current Timer:** `{TIMER_SECONDS}` Seconds\n"
        f"👥 **Group Lock Status:** {'Active' if ALLOWED_CHAT_IDS else 'All Groups Allowed'}\n\n"
        f"Naya timer choose karne ke liye neeche click karein:"
    )
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
        f"🆔 **Chat ID:** `{message.chat.id}`\n\n"
        f"_Ab is group me quiz successfully chalega._"
    )


@app.on_message(filters.command("ca") & filters.group)
async def exam_quiz_handler(client: Client, message: Message):
    # 1. Allowed Group Restriction
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        return

    # 2. Strict Owner Check
    if not await is_owner(client, message.chat.id, message.from_user.id):
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
            question=f"⏱️️ [Timer: {TIMER_SECONDS}s]\n" + data["question"],
            options=options,
            is_anonymous=False,
            type="quiz",
            correct_option_id=int(data["correct_option_id"]),
            explanation=data.get("explanation", ""),
            open_period=TIMER_SECONDS
        )
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ Error: Question generate nahi ho saka.\n`{e}`")


# ==================== CALLBACK BUTTON HANDLERS ====================

@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    global TIMER_SECONDS
    data = query.data

    if data == "btn_help":
        await query.answer()
        help_text = (
            "📚 **Bot Command Guide:**\n\n"
            "🔹 `/ca` - Latest Current Affairs Question\n"
            "🔹 `/ca <topic>` - Specific Topic CA Question\n"
            "🔹 `/settings` - Timer Badalne Ke Liye\n"
            "🔹 `/setgroup` - Group Lock Set Karne Ke Liye"
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
        await query.message.edit_text(f"✅ **Timer successfully updated:** `{TIMER_SECONDS}` Seconds")


# ==================== BOT RUNNER ====================

async def set_menu_commands():
    """Bot start hone ke baad menu suggestions set karna."""
    await asyncio.sleep(2)
    try:
        commands = [
            BotCommand("ca", "Start Current Affairs Quiz (Owner Only)"),
            BotCommand("settings", "Configure Quiz Timer & Settings"),
            BotCommand("setgroup", "Authorize this group for quizzes"),
            BotCommand("help", "Show help and command guide"),
            BotCommand("start", "Start the bot interface")
        ]
        await app.set_bot_commands(commands)
        print("✅ Telegram Command Menu Suggestions set ho gaye!")
    except Exception as e:
        print(f"⚠️ Command menu set karne me error: {e}")


if __name__ == "__main__":
    print("Owner CA Quiz Bot start ho raha hai...")
    # Background task for command menu suggestions
    app.loop.create_task(set_menu_commands())
    app.run()
