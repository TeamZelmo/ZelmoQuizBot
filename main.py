import os
import re
import json
import time
import asyncio
from aiohttp import web
from groq import AsyncGroq
from pyrogram import Client, filters, idle, raw
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.types import (
    Message,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from dotenv import load_dotenv

load_dotenv()

# --- कॉन्फ़िग ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

TIMER_SECONDS = int(os.getenv("TIMER_SECONDS", "30"))
MAX_QUESTIONS = 25

# env में कॉमा से अलग किए नाम, जैसे GROQ_MODELS="openai/gpt-oss-120b,openai/gpt-oss-20b"
GROQ_MODELS = [
    m.strip()
    for m in os.getenv("GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",")
    if m.strip()
]

raw_chats = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [int(cid.strip()) for cid in raw_chats.split(",") if cid.strip()]

app = Client(
    "competition_ca_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# chat_id -> सत्र (session)
SESSIONS = {}
# poll_id (संख्या) -> chat_id
POLL_TO_CHAT = {}


async def is_owner(client: Client, chat_id: int, user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status == ChatMemberStatus.OWNER
    except Exception as e:
        print(f"⚠️ स्वामी की जाँच में त्रुटि: {e}")
        return False


def clean_name(name: str) -> str:
    """मार्कडाउन तोड़ने वाले अक्षर हटाना।"""
    return re.sub(r"[*_`\[\]~]", "", name).strip()


async def safe_send(client: Client, chat_id: int, text: str):
    """संदेश भेजना; फ़ॉर्मेटिंग में दिक्कत हो तो सादे रूप में भेजना।"""
    try:
        await client.send_message(chat_id, text)
    except Exception as e:
        print(f"⚠️ फ़ॉर्मेटेड संदेश नहीं गया, सादा भेज रहे हैं: {e}")
        plain = text.replace("**", "").replace("`", "")
        await client.send_message(chat_id, plain, parse_mode=ParseMode.DISABLED)


async def generate_universal_quiz(user_topic: str, avoid: list = None) -> dict:
    avoid_text = ""
    if avoid:
        avoid_text = "\n    ये प्रश्न पहले पूछे जा चुके हैं, इन्हें दोहराएँ नहीं:\n    - " + "\n    - ".join(avoid[-15:])

    prompt = f"""
    You are an expert exam setter for Indian competitive exams (UPSC, SSC CGL/CHSL, BPSC, State PCS, Banking, Railway, etc.).

    Target Topic / Subject / Exam: "{user_topic}"

    Instructions:
    1. Create exactly 1 high-quality, authentic Multiple Choice Question (MCQ) strictly matching the requested topic/exam syllabus.
    2. If the user mentions a specific exam (e.g. BPSC, UPSC, SSC), strictly match that exam's difficulty and standard.
    3. If the topic is static (History, Polity, Geography, Science, Math, Reasoning, Bihar Special), frame a concept-based or factually accurate question.
    4. If the topic is Current Affairs, focus on real verified developments, schemes, indices, or appointments.
    5. LANGUAGE: Write the question, all options and the explanation in pure, standard Hindi using Devanagari script (शुद्ध हिंदी). Do not use Roman/Hinglish. Use English only where an official abbreviation is unavoidable.
    6. Keep question under 220 characters, each option under 90 characters, explanation under 180 characters.
    7. Provide 4 distinct options and 1 concise factual explanation.
    8. Return RAW JSON ONLY without any markdown backticks.{avoid_text}

    Required JSON Schema:
    {{
      "question": "प्रश्न यहाँ",
      "options": ["विकल्प क", "विकल्प ख", "विकल्प ग", "विकल्प घ"],
      "correct_option_id": 0,
      "explanation": "1-2 पंक्तियों में स्पष्ट व्याख्या"
    }}
    """

    errors = []
    for model_name in GROQ_MODELS:
        try:
            chat_completion = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=model_name,
                temperature=0.5,
                response_format={"type": "json_object"}
            )
            content = chat_completion.choices[0].message.content.strip()
            content = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(content)

            options = [str(o)[:100] for o in data["options"][:4]]
            cid = int(data["correct_option_id"])
            if len(options) < 2 or not (0 <= cid < len(options)):
                raise ValueError("मॉडल से अमान्य प्रश्न मिला")
            data["options"] = options
            data["correct_option_id"] = cid
            return data
        except Exception as e:
            print(f"⚠️ मॉडल {model_name} विफल: {e}। अगला मॉडल आज़मा रहे हैं...")
            errors.append(f"{model_name}: {str(e)[:120]}")

    raise RuntimeError(" | ".join(errors))


# ==================== परिणाम तालिका ====================

def build_leaderboard(session: dict, finished: bool = True) -> str:
    total = session["asked"]
    scores = session["scores"]

    if not scores:
        return "😶 किसी ने भी उत्तर नहीं दिया, इसलिए परिणाम तालिका खाली है।"

    rows = []
    for uid, s in scores.items():
        attempted = s["correct"] + s["wrong"]
        skipped = max(total - attempted, 0)
        rows.append((uid, s, skipped))

    # अधिक सही उत्तर वाला ऊपर; बराबरी पर कम कुल समय वाला ऊपर
    rows.sort(key=lambda r: (-r[1]["correct"], r[1]["time"]))

    rank_labels = ["🥇 प्रथम स्थान", "🥈 द्वितीय स्थान", "🥉 तृतीय स्थान"]
    title = "🏆 **अंतिम परिणाम**" if finished else "📊 **अब तक का परिणाम**"
    lines = [f"{title}\n📝 कुल प्रश्न: `{total}`\n"]

    for i, (uid, s, skipped) in enumerate(rows):
        label = rank_labels[i] if i < 3 else f"{i + 1}. स्थान"
        lines.append(
            f"{label} — **{s['name']}**\n"
            f"    ✅ सही: `{s['correct']}`   ❌ गलत: `{s['wrong']}`   ⏭ छोड़े गए: `{skipped}`\n"
            f"    ⏱ कुल समय: `{s['time']:.1f}` सेकंड"
        )

    lines.append(f"\n👑 **विजेता (प्रथम):** {rows[0][1]['name']}")
    if len(rows) > 1:
        lines.append(f"🥈 **द्वितीय:** {rows[1][1]['name']}")
    if len(rows) > 2:
        lines.append(f"🥉 **तृतीय:** {rows[2][1]['name']}")
    lines.append("\n🙏 सभी प्रतिभागियों का धन्यवाद!")
    return "\n".join(lines)


async def run_quiz(client: Client, chat_id: int, topic: str, total: int):
    session = SESSIONS[chat_id]
    asked_questions = []
    next_task = None

    try:
        next_task = asyncio.create_task(generate_universal_quiz(topic, asked_questions))

        for i in range(1, total + 1):
            try:
                data = await next_task
            except Exception as e:
                await safe_send(client, chat_id, f"❌ प्रश्न {i} नहीं बन सका।\n`{str(e)[:300]}`")
                if i < total:
                    next_task = asyncio.create_task(generate_universal_quiz(topic, asked_questions))
                continue

            asked_questions.append(data["question"][:120])

            # अगला प्रश्न अभी से पृष्ठभूमि में तैयार करना
            if i < total:
                next_task = asyncio.create_task(generate_universal_quiz(topic, asked_questions))

            question_text = f"[{i}/{total}] ⏱{TIMER_SECONDS} सेकंड | {data['question']}"[:300]
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

            # महत्वपूर्ण: Pyrogram में poll.id टेक्स्ट होता है, वोट अपडेट में संख्या; इसलिए int में बदलें
            poll_id = int(sent.poll.id)
            POLL_TO_CHAT[poll_id] = chat_id
            session["polls"][poll_id] = {
                "correct": data["correct_option_id"],
                "sent_at": time.time(),
                "voted": set()
            }
            session["asked"] += 1

            # समय पूरा होने तक रुकें, उसके बाद ही अगला प्रश्न
            await asyncio.sleep(TIMER_SECONDS + 1)

        await safe_send(client, chat_id, build_leaderboard(session, finished=True))

    except asyncio.CancelledError:
        await safe_send(client, chat_id, "🛑 **प्रश्नोत्तरी रोक दी गई।**\n\n" + build_leaderboard(session, finished=False))
        raise
    except Exception as e:
        print(f"❌ प्रश्नोत्तरी में त्रुटि: {e}")
        await safe_send(client, chat_id, f"❌ प्रश्नोत्तरी में त्रुटि आई।\n`{str(e)[:300]}`\n\n" + build_leaderboard(session, finished=False))
    finally:
        if next_task and not next_task.done():
            next_task.cancel()
        for pid in list(session["polls"].keys()):
            POLL_TO_CHAT.pop(pid, None)
        SESSIONS.pop(chat_id, None)


# ==================== वोट की गिनती ====================

@app.on_raw_update(group=-1)
async def vote_tracker(client, update, users, chats):
    if not isinstance(update, raw.types.UpdateMessagePollVote):
        return

    try:
        chat_id = POLL_TO_CHAT.get(int(update.poll_id))
        if chat_id is None:
            return

        session = SESSIONS.get(chat_id)
        if not session:
            return

        poll = session["polls"].get(int(update.poll_id))
        if not poll or not update.options:
            return

        user_id = getattr(update.peer, "user_id", None)
        if user_id is None or user_id in poll["voted"]:
            return
        poll["voted"].add(user_id)

        chosen = update.options[0][0]  # विकल्प का क्रमांक
        elapsed = time.time() - poll["sent_at"]

        user = users.get(user_id)
        if user:
            full = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
            name = clean_name(full) or f"उपयोगकर्ता {user_id}"
        else:
            name = f"उपयोगकर्ता {user_id}"

        s = session["scores"].setdefault(user_id, {"name": name, "correct": 0, "wrong": 0, "time": 0.0})
        if chosen == poll["correct"]:
            s["correct"] += 1
        else:
            s["wrong"] += 1
        s["time"] += elapsed
    except Exception as e:
        print(f"⚠️ वोट दर्ज करने में त्रुटि: {e}")


# ==================== डमी वेब सर्वर ====================

async def handle_ping(request):
    return web.Response(text="बॉट सक्रिय है!")


async def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = web.Application()
    server.router.add_get("/", handle_ping)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 वेब सर्वर पोर्ट {port} पर चालू है")


# ==================== कमांड ====================

def timer_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("15 सेकंड", callback_data="set_time_15"),
            InlineKeyboardButton("30 सेकंड", callback_data="set_time_30"),
            InlineKeyboardButton("45 सेकंड", callback_data="set_time_45"),
            InlineKeyboardButton("60 सेकंड", callback_data="set_time_60")
        ]
    ])


@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    text = (
        "🙏 **नमस्कार! मैं प्रतियोगी परीक्षा प्रश्नोत्तरी बॉट हूँ।**\n\n"
        "🎯 UPSC, SSC, BPSC, रेलवे, बैंकिंग, समसामयिकी, किसी भी विषय की प्रश्नोत्तरी चलाएँ।\n\n"
        "उदाहरण: `/ca 10 बिहार का इतिहास`\n\n"
        "📖 सभी आदेश जानने के लिए **/help** दबाएँ।\n"
        "⚠️ कृत्रिम बुद्धिमत्ता से बने उत्तरों की एक बार पुष्टि अवश्य कर लें।"
    )
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 सहायता", callback_data="btn_help"),
            InlineKeyboardButton("⚙️ व्यवस्थाएँ", callback_data="btn_settings")
        ]
    ])
    await message.reply_text(text, reply_markup=buttons)


@app.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message):
    help_text = (
        "📚 **आदेश मार्गदर्शिका:**\n\n"
        f"🔹 `/ca <संख्या> <विषय>` — कई प्रश्नों की प्रश्नोत्तरी (अधिकतम {MAX_QUESTIONS})\n"
        "   • `/ca 10 बिहार विशेष इतिहास`\n"
        "   • `/ca 5 भारतीय राजव्यवस्था`\n"
        "   • `/ca 15 सामान्य विज्ञान`\n"
        "   • `/ca` — 1 प्रश्न (मिश्रित सामान्य ज्ञान)\n\n"
        "🔹 `/score` — चल रही प्रश्नोत्तरी की अब तक की परिणाम तालिका\n"
        "🔹 `/stopquiz` — चल रही प्रश्नोत्तरी रोकें (केवल स्वामी)\n"
        "🔹 `/settings` — समय-सीमा बदलें\n"
        "🔹 `/setgroup` — समूह को अधिकृत करें (केवल स्वामी)\n"
        "🔹 `/id` — चैट और उपयोगकर्ता की पहचान संख्या"
    )
    await message.reply_text(help_text)


@app.on_message(filters.command("id"))
async def id_handler(client: Client, message: Message):
    if not message.from_user:
        return
    await message.reply_text(
        f"📌 **चैट आईडी:** `{message.chat.id}`\n"
        f"👤 **उपयोगकर्ता आईडी:** `{message.from_user.id}`"
    )


@app.on_message(filters.command("settings"))
async def settings_handler(client: Client, message: Message):
    if not message.from_user:
        return
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **यह आदेश केवल स्वामी के लिए है!**")
        return

    text = f"⚙️ **प्रश्नोत्तरी व्यवस्थाएँ:**\n\n⏱️ **वर्तमान समय-सीमा:** `{TIMER_SECONDS}` सेकंड\nनई समय-सीमा चुनें:"
    await message.reply_text(text, reply_markup=timer_buttons())


@app.on_message(filters.command("setgroup") & filters.group)
async def setgroup_handler(client: Client, message: Message):
    if not message.from_user:
        return
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **केवल स्वामी ही समूह अधिकृत कर सकता है!**")
        return

    if message.chat.id not in ALLOWED_CHAT_IDS:
        ALLOWED_CHAT_IDS.append(message.chat.id)

    await message.reply_text(
        f"✅ **समूह सफलतापूर्वक अधिकृत हो गया!**\n\n"
        f"📌 **समूह:** {message.chat.title}\n"
        f"🆔 **चैट आईडी:** `{message.chat.id}`"
    )


@app.on_message(filters.command("ca"))
async def exam_quiz_handler(client: Client, message: Message):
    if not message.from_user:
        return
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        return

    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **यह आदेश केवल समूह स्वामी के लिए सुरक्षित है!**")
        return

    chat_id = message.chat.id
    if chat_id in SESSIONS:
        await message.reply_text("⚠️ इस चैट में प्रश्नोत्तरी पहले से चल रही है। रोकने के लिए /stopquiz का प्रयोग करें।")
        return

    args = message.command[1:]
    total = 1
    if args and args[0].isdigit():
        total = max(1, min(int(args[0]), MAX_QUESTIONS))
        args = args[1:]

    topic = " ".join(args) if args else "UPSC/SSC/BPSC के लिए सामान्य ज्ञान और हाल की समसामयिकी का मिश्रण"

    SESSIONS[chat_id] = {
        "scores": {},
        "polls": {},
        "asked": 0,
        "task": None
    }

    await message.reply_text(
        f"🎯 **प्रश्नोत्तरी आरंभ!**\n\n"
        f"📚 विषय: `{topic}`\n"
        f"📝 कुल प्रश्न: `{total}`\n"
        f"⏱ प्रत्येक प्रश्न का समय: `{TIMER_SECONDS}` सेकंड\n\n"
        f"तैयार हो जाइए, पहला प्रश्न आ रहा है..."
    )

    SESSIONS[chat_id]["task"] = asyncio.create_task(run_quiz(client, chat_id, topic, total))


@app.on_message(filters.command("stopquiz"))
async def stop_handler(client: Client, message: Message):
    if not message.from_user:
        return
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ केवल स्वामी ही प्रश्नोत्तरी रोक सकता है!")
        return

    session = SESSIONS.get(message.chat.id)
    if not session:
        await message.reply_text("ℹ️ अभी कोई प्रश्नोत्तरी नहीं चल रही।")
        return
    session["task"].cancel()


@app.on_message(filters.command("score"))
async def score_handler(client: Client, message: Message):
    session = SESSIONS.get(message.chat.id)
    if not session:
        await message.reply_text("ℹ️ अभी कोई प्रश्नोत्तरी नहीं चल रही।")
        return
    await safe_send(client, message.chat.id, build_leaderboard(session, finished=False))


# ==================== बटन ====================

@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    global TIMER_SECONDS
    data = query.data

    if data == "btn_help":
        await query.answer()
        await query.message.reply_text(
            "🔹 `/ca <संख्या> <विषय>` — प्रश्नोत्तरी चलाएँ\n"
            "🔹 `/score` — अब तक का परिणाम\n"
            "🔹 `/stopquiz` — प्रश्नोत्तरी रोकें\n"
            "🔹 `/settings` — समय-सीमा बदलें\n"
            "🔹 `/id` — चैट आईडी देखें"
        )

    elif data == "btn_settings":
        if not await is_owner(client, query.message.chat.id, query.from_user.id):
            await query.answer("⛔ केवल स्वामी व्यवस्थाएँ बदल सकता है!", show_alert=True)
            return
        await query.answer()
        await query.message.reply_text("⏱️ समय-सीमा चुनें:", reply_markup=timer_buttons())

    elif data.startswith("set_time_"):
        if not await is_owner(client, query.message.chat.id, query.from_user.id):
            await query.answer("⛔ केवल स्वामी समय-सीमा बदल सकता है!", show_alert=True)
            return
        TIMER_SECONDS = int(data.split("_")[2])
        await query.answer(f"✅ समय-सीमा {TIMER_SECONDS} सेकंड कर दी गई!", show_alert=True)
        await query.message.edit_text(f"✅ **समय-सीमा अद्यतन:** `{TIMER_SECONDS}` सेकंड")


# ==================== आरंभ ====================

async def send_startup_alert():
    target_id = LOG_GROUP_ID if LOG_GROUP_ID else OWNER_ID
    if not target_id:
        print("⚠️ LOG_GROUP_ID और OWNER_ID दोनों निर्धारित नहीं हैं। सूचना छोड़ दी गई।")
        return
    try:
        bot_info = await app.get_me()
        await app.send_message(
            chat_id=target_id,
            text=(
                "🚀 **प्रश्नोत्तरी बॉट सफलतापूर्वक चालू हो गया!**\n\n"
                f"🤖 **बॉट:** @{bot_info.username}\n"
                f"⏱️ **डिफ़ॉल्ट समय-सीमा:** `{TIMER_SECONDS}` सेकंड\n"
                "⚡ **स्थिति:** सक्रिय और तैयार!"
            )
        )
    except Exception as e:
        print(f"❌ आरंभ सूचना में त्रुटि (लक्ष्य आईडी: {target_id}): {e}")


async def set_menu_suggestions():
    await asyncio.sleep(2)
    try:
        await app.set_bot_commands([
            BotCommand("ca", "प्रश्नोत्तरी चलाएँ: /ca 10 विषय"),
            BotCommand("score", "अब तक का परिणाम देखें"),
            BotCommand("stopquiz", "चल रही प्रश्नोत्तरी रोकें"),
            BotCommand("settings", "समय-सीमा की व्यवस्था"),
            BotCommand("setgroup", "समूह को अधिकृत करें"),
            BotCommand("id", "चैट और उपयोगकर्ता आईडी"),
            BotCommand("help", "सहायता मार्गदर्शिका"),
            BotCommand("start", "बॉट आरंभ करें")
        ])
        print("✅ आदेश सूची निर्धारित हो गई!")
    except Exception as e:
        print(f"⚠️ आदेश सूची चेतावनी: {e}")


async def main():
    await start_dummy_server()
    print("🚀 टेलीग्राम से जुड़ रहे हैं...")
    await app.start()
    print("✅ बॉट जुड़ गया और संदेशों की प्रतीक्षा में है!")
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
