import os
import json
import time
import asyncio
from aiohttp import web
from groq import AsyncGroq
from pyrogram import Client, filters, idle, raw
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
MAX_QUESTIONS = 25

raw_chats = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [int(cid.strip()) for cid in raw_chats.split(",") if cid.strip()]

app = Client(
    "competition_ca_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# chat_id -> session dict
SESSIONS = {}
# poll_id -> chat_id
POLL_TO_CHAT = {}


async def is_owner(client: Client, chat_id: int, user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status == ChatMemberStatus.OWNER
    except Exception as e:
        print(f"⚠️ Error checking owner status: {e}")
        return False


async def generate_universal_quiz(user_topic: str, avoid: list = None) -> dict:
    avoid_text = ""
    if avoid:
        avoid_text = "\n    Do NOT repeat these already-asked questions:\n    - " + "\n    - ".join(avoid[-15:])

    prompt = f"""
    You are an expert exam setter for Indian competitive exams (UPSC, SSC CGL/CHSL, BPSC, State PCS, Banking, Railway, etc.).

    Target Topic / Subject / Exam: "{user_topic}"

    Instructions:
    1. Create exactly 1 high-quality, authentic Multiple Choice Question (MCQ) strictly matching the requested topic/exam syllabus.
    2. If the user mentions a specific exam (e.g. BPSC, UPSC, SSC), strictly match that exam's difficulty and standard.
    3. If the topic is static (History, Polity, Geography, Science, Math, Reasoning, Bihar Special), frame a concept-based or factually accurate question.
    4. If the topic is Current Affairs, focus on real verified developments, schemes, indices, or appointments.
    5. Language: Bilingual/Hinglish (Hindi + English key terms).
    6. Keep question under 220 characters, each option under 90 characters, explanation under 180 characters.
    7. Provide 4 distinct options and 1 concise factual explanation.
    8. Return RAW JSON ONLY without any markdown backticks.{avoid_text}

    Required JSON Schema:
    {{
      "question": "Question text here",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct_option_id": 0,
      "explanation": "Clear 1-2 line explanation"
    }}
    """

    stable_models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

    last_error = None
    for model_name in stable_models:
        try:
            chat_completion = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=model_name,
                temperature=0.5,
                response_format={"type": "json_object"}
            )
            data = json.loads(chat_completion.choices[0].message.content.strip())

            options = [str(o)[:100] for o in data["options"][:4]]
            cid = int(data["correct_option_id"])
            if len(options) < 2 or not (0 <= cid < len(options)):
                raise ValueError("Invalid quiz data from model")
            data["options"] = options
            data["correct_option_id"] = cid
            return data
        except Exception as e:
            print(f"⚠️ Model {model_name} failed: {e}. Trying fallback...")
            last_error = e

    raise last_error


# ==================== SCOREBOARD ====================

def build_leaderboard(session: dict, finished: bool = True) -> str:
    total = session["asked"]
    scores = session["scores"]

    if not scores:
        return "😶 Kisi ne bhi answer nahi diya, isliye leaderboard khali hai."

    rows = []
    for uid, s in scores.items():
        attempted = s["correct"] + s["wrong"]
        skipped = total - attempted
        rows.append((uid, s, skipped))

    # Zyada sahi -> upar. Tie ho to kam total time wala upar.
    rows.sort(key=lambda r: (-r[1]["correct"], r[1]["time"]))

    medals = ["🥇", "🥈", "🥉"]
    title = "🏆 **Final Leaderboard**" if finished else "📊 **Current Leaderboard**"
    lines = [f"{title}\n📝 Total Questions: `{total}`\n"]

    for i, (uid, s, skipped) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        lines.append(
            f"{medal} **{s['name']}**\n"
            f"    ✅ Sahi: `{s['correct']}`  ❌ Galat: `{s['wrong']}`  ⏭ Skip: `{skipped}`\n"
            f"    ⏱ Total time: `{s['time']:.1f}s`"
        )

    first = rows[0][1]["name"]
    lines.append(f"\n👑 **Winner:** {first}")
    if len(rows) > 1:
        lines.append(f"🥈 **Second:** {rows[1][1]['name']}")
    return "\n".join(lines)


async def run_quiz(client: Client, chat_id: int, topic: str, total: int):
    session = SESSIONS[chat_id]
    asked_questions = []

    try:
        for i in range(1, total + 1):
            try:
                data = await generate_universal_quiz(topic, asked_questions)
            except Exception as e:
                await client.send_message(chat_id, f"❌ Question {i} generate nahi ho saka: `{e}`")
                continue

            asked_questions.append(data["question"][:120])

            question_text = f"[{i}/{total}] ⏱{TIMER_SECONDS}s | {data['question']}"[:300]
            sent = await client.send_poll(
                chat_id=chat_id,
                question=question_text,
                options=data["options"],
                is_anonymous=False,
                type="quiz",
                correct_option_id=data["correct_option_id"],
                explanation=data.get("explanation", "")[:200],
                open_period=TIMER_SECONDS
            )

            poll_id = sent.poll.id
            POLL_TO_CHAT[poll_id] = chat_id
            session["polls"][poll_id] = {
                "correct": data["correct_option_id"],
                "sent_at": time.time(),
                "voted": set()
            }
            session["asked"] += 1

            # Poll band hone tak + 2 sec ruko
            await asyncio.sleep(TIMER_SECONDS + 2)

        await client.send_message(chat_id, build_leaderboard(session, finished=True))

    except asyncio.CancelledError:
        await client.send_message(chat_id, "🛑 **Quiz rok diya gaya.**\n\n" + build_leaderboard(session, finished=False))
        raise
    finally:
        for pid in list(session["polls"].keys()):
            POLL_TO_CHAT.pop(pid, None)
        SESSIONS.pop(chat_id, None)


# ==================== VOTE TRACKING ====================

@app.on_raw_update(group=-1)
async def vote_tracker(client, update, users, chats):
    if not isinstance(update, raw.types.UpdateMessagePollVote):
        return

    chat_id = POLL_TO_CHAT.get(update.poll_id)
    if chat_id is None:
        return

    session = SESSIONS.get(chat_id)
    if not session:
        return

    poll = session["polls"].get(update.poll_id)
    if not poll or not update.options:
        return

    user_id = getattr(update.peer, "user_id", None)
    if user_id is None or user_id in poll["voted"]:
        return
    poll["voted"].add(user_id)

    chosen = update.options[0][0]  # option index (single byte)
    elapsed = time.time() - poll["sent_at"]

    user = users.get(user_id)
    if user:
        name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
        name = name.strip() or f"User {user_id}"
    else:
        name = f"User {user_id}"

    s = session["scores"].setdefault(user_id, {"name": name, "correct": 0, "wrong": 0, "time": 0.0})
    if chosen == poll["correct"]:
        s["correct"] += 1
    else:
        s["wrong"] += 1
    s["time"] += elapsed


# ==================== DUMMY WEB SERVER ====================

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
    print(f"🌐 Dummy web server listening on port {port}")


# ==================== COMMAND HANDLERS ====================

@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    text = (
        "👋 **Namaste! Main Universal Competition Quiz Bot hoon.**\n\n"
        "🎯 UPSC, SSC, BPSC, Railway, Banking, Current Affairs, kisi bhi topic ka quiz chalayein.\n\n"
        "Example: `/ca 10 bpsc bihar history`\n\n"
        "📖 Commands ke liye **/help** dabayein.\n"
        "⚠️ AI se bane answers ko ek baar verify zaroor karein."
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
        "📚 **Quiz Command Guide:**\n\n"
        f"🔹 `/ca <kitne> <topic>` - Multiple questions ka quiz (max {MAX_QUESTIONS})\n"
        "   • `/ca 10 bpsc bihar special history`\n"
        "   • `/ca 5 upsc polity`\n"
        "   • `/ca 15 ssc cgl science`\n"
        "   • `/ca` - 1 question (mixed GS)\n\n"
        "🔹 `/score` - Chalte quiz ka current leaderboard\n"
        "🔹 `/stopquiz` - Chalta quiz rokein (Owner only)\n"
        "🔹 `/settings` - Timer customize karein\n"
        "🔹 `/setgroup` - Group authorize karein (Owner only)\n"
        "🔹 `/id` - Chat ID aur User ID"
    )
    await message.reply_text(help_text)


@app.on_message(filters.command("id"))
async def id_handler(client: Client, message: Message):
    if not message.from_user:
        return
    await message.reply_text(
        f"📌 **Chat ID:** `{message.chat.id}`\n"
        f"👤 **User ID:** `{message.from_user.id}`"
    )


@app.on_message(filters.command("settings"))
async def settings_handler(client: Client, message: Message):
    if not message.from_user:
        return
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
    if not message.from_user:
        return
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
    if not message.from_user:
        return
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        return

    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Yeh command sirf Group Owner ke liye reserved hai!**")
        return

    chat_id = message.chat.id
    if chat_id in SESSIONS:
        await message.reply_text("⚠️ Is chat me quiz pehle se chal raha hai. Rokne ke liye /stopquiz use karein.")
        return

    args = message.command[1:]
    total = 1
    if args and args[0].isdigit():
        total = max(1, min(int(args[0]), MAX_QUESTIONS))
        args = args[1:]

    topic = " ".join(args) if args else "Mix GS and Recent Current Affairs for UPSC/SSC/BPSC"

    SESSIONS[chat_id] = {
        "scores": {},
        "polls": {},
        "asked": 0,
        "task": None
    }

    await message.reply_text(
        f"🎯 **Quiz Start!**\n\n"
        f"📚 Topic: `{topic}`\n"
        f"📝 Questions: `{total}`\n"
        f"⏱ Har question: `{TIMER_SECONDS}s`\n\n"
        f"Taiyar ho jaiye, pehla question aa raha hai..."
    )

    SESSIONS[chat_id]["task"] = asyncio.create_task(run_quiz(client, chat_id, topic, total))


@app.on_message(filters.command("stopquiz"))
async def stop_handler(client: Client, message: Message):
    if not message.from_user:
        return
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ Sirf Owner quiz rok sakta hai!")
        return

    session = SESSIONS.get(message.chat.id)
    if not session:
        await message.reply_text("ℹ️ Abhi koi quiz nahi chal raha.")
        return
    session["task"].cancel()


@app.on_message(filters.command("score"))
async def score_handler(client: Client, message: Message):
    session = SESSIONS.get(message.chat.id)
    if not session:
        await message.reply_text("ℹ️ Abhi koi quiz nahi chal raha.")
        return
    await message.reply_text(build_leaderboard(session, finished=False))


# ==================== CALLBACK BUTTON HANDLERS ====================

@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    global TIMER_SECONDS
    data = query.data

    if data == "btn_help":
        await query.answer()
        await query.message.reply_text(
            "🔹 `/ca <kitne> <topic>` - Quiz chalayein\n"
            "🔹 `/score` - Current leaderboard\n"
            "🔹 `/stopquiz` - Quiz rokein\n"
            "🔹 `/settings` - Timer badalein\n"
            "🔹 `/id` - Chat ID check karein"
        )

    elif data == "btn_settings":
        if not await is_owner(client, query.message.chat.id, query.from_user.id):
            await query.answer("⛔ Sirf Owner settings badal sakta hai!", show_alert=True)
            return
        await query.answer()
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
        TIMER_SECONDS = int(data.split("_")[2])
        await query.answer(f"✅ Timer {TIMER_SECONDS}s par set ho gaya!", show_alert=True)
        await query.message.edit_text(f"✅ **Timer updated:** `{TIMER_SECONDS}` Seconds")


# ==================== STARTUP ====================

async def send_startup_alert():
    target_id = LOG_GROUP_ID if LOG_GROUP_ID else OWNER_ID
    if not target_id:
        print("⚠️ LOG_GROUP_ID aur OWNER_ID dono set nahi hain. Startup alert skip.")
        return
    try:
        bot_info = await app.get_me()
        await app.send_message(
            chat_id=target_id,
            text=(
                "🚀 **Universal Quiz Bot Started!**\n\n"
                f"🤖 **Bot:** @{bot_info.username}\n"
                f"⏱️ **Default Timer:** `{TIMER_SECONDS}s`\n"
                "⚡ **Status:** Active & Ready!"
            )
        )
    except Exception as e:
        print(f"❌ Startup alert error (Target ID: {target_id}): {e}")


async def set_menu_suggestions():
    await asyncio.sleep(2)
    try:
        await app.set_bot_commands([
            BotCommand("ca", "Quiz chalayein: /ca 10 topic"),
            BotCommand("score", "Current leaderboard dekhein"),
            BotCommand("stopquiz", "Chalta quiz rokein"),
            BotCommand("settings", "Quiz Timer settings"),
            BotCommand("setgroup", "Group authorize karein"),
            BotCommand("id", "Group aur User ID"),
            BotCommand("help", "Help guide"),
            BotCommand("start", "Bot start karein")
        ])
        print("✅ Command menu set!")
    except Exception as e:
        print(f"⚠️ Suggestions set warning: {e}")


async def main():
    await start_dummy_server()
    print("🚀 Connecting Pyrogram Client to Telegram...")
    await app.start()
    print("✅ Pyrogram Client Connected!")
    await send_startup_alert()
    asyncio.create_task(set_menu_suggestions())
    await idle()
    await app.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        pass
