import os
import re
import json
import time
import asyncio
from aiohttp import web
from groq import AsyncGroq
from pyrogram import Client, filters, idle, raw
from pyrogram.enums import ChatMemberStatus, ParseMode, PollType
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


def clean(text: str) -> str:
    """मार्कडाउन तोड़ने वाले अक्षर हटाना।"""
    return re.sub(r"[*_`\[\]~]", "", str(text)).strip()


async def safe_send(client: Client, chat_id: int, text: str, **kwargs):
    """संदेश भेजना; फ़ॉर्मेटिंग में दिक्कत हो तो सादे रूप में भेजना।"""
    try:
        return await client.send_message(chat_id, text, **kwargs)
    except Exception as e:
        print(f"⚠️ फ़ॉर्मेटेड संदेश नहीं गया, सादा भेज रहे हैं: {e}")
        plain = text.replace("**", "").replace("`", "")
        return await client.send_message(chat_id, plain, parse_mode=ParseMode.DISABLED, **kwargs)


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
    7. Provide exactly 4 distinct options and 1 concise factual explanation.
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

            options = [str(o).strip()[:100] for o in data["options"][:4]]
            cid = int(data["correct_option_id"])
            if len(options) < 2 or not (0 <= cid < len(options)):
                raise ValueError("मॉडल से अमान्य प्रश्न मिला")
            data["question"] = str(data["question"]).strip()
            data["explanation"] = str(data.get("explanation", "")).strip()
            data["options"] = options
            data["correct_option_id"] = cid
            return data
        except Exception as e:
            print(f"⚠️ मॉडल {model_name} विफल: {e}। अगला मॉडल आज़मा रहे हैं...")
            errors.append(f"{model_name}: {str(e)[:120]}")

    raise RuntimeError(" | ".join(errors))


# ==================== वोट दर्ज करना ====================

def record_vote(session: dict, q: dict, uid: int, name: str, chosen: int, elapsed: float):
    """एक उपयोगकर्ता का एक प्रश्न का उत्तर दर्ज करना (दोहराव से बचाव सहित)।"""
    if uid in q["voted"]:
        return
    is_right = (chosen == q["correct"])
    q["voted"][uid] = is_right

    s = session["scores"].setdefault(uid, {"name": name, "correct": 0, "wrong": 0, "time": 0.0})
    if is_right:
        s["correct"] += 1
    else:
        s["wrong"] += 1
    s["time"] += max(elapsed, 0.0)


def raw_user_name(user, uid: int) -> str:
    if user:
        full = (getattr(user, "first_name", "") or "")
        last = getattr(user, "last_name", "") or ""
        if last:
            full += " " + last
        full = clean(full)
        if full:
            return full
    return f"उपयोगकर्ता {uid}"


@app.on_raw_update(group=-1)
async def vote_tracker(client, update, users, chats):
    """वोट आते ही तुरंत दर्ज करना।"""
    if not isinstance(update, raw.types.UpdateMessagePollVote):
        return

    try:
        poll_id = int(update.poll_id)
        chat_id = POLL_TO_CHAT.get(poll_id)
        if chat_id is None:
            return

        session = SESSIONS.get(chat_id)
        if not session:
            return

        q = session["polls"].get(poll_id)
        if not q or not update.options:
            return

        user_id = getattr(update.peer, "user_id", None)
        if user_id is None:
            return

        chosen = update.options[0][0]  # विकल्प का क्रमांक
        elapsed = time.time() - q["sent_at"]
        name = raw_user_name(users.get(user_id), user_id)
        print(f"🗳 वोट मिला: प्रश्न {q['no']}, {name}, विकल्प {chosen}")
        record_vote(session, q, user_id, name, chosen, elapsed)
    except Exception as e:
        print(f"⚠️ वोट दर्ज करने में त्रुटि: {e}")


async def reconcile_votes(client: Client, chat_id: int, q: dict, session: dict):
    """समय पूरा होने पर टेलीग्राम से मतदाताओं की सूची निकालकर छूटे हुए वोट जोड़ना।"""
    try:
        peer = await client.resolve_peer(chat_id)
        for k in range(q["n_options"]):
            res = await client.invoke(
                raw.functions.messages.GetPollVotes(
                    peer=peer,
                    id=q["message_id"],
                    limit=100,
                    option=bytes([k])
                )
            )
            user_map = {u.id: u for u in res.users}
            for v in res.votes:
                uid = getattr(v.peer, "user_id", None)
                if uid is None:
                    continue
                vote_date = getattr(v, "date", None)
                elapsed = (vote_date - int(q["sent_at"])) if isinstance(vote_date, int) else q["limit"]
                name = raw_user_name(user_map.get(uid), uid)
                record_vote(session, q, uid, name, k, elapsed)
    except Exception as e:
        print(f"⚠️ मतदाता सूची से मिलान में दिक्कत (live वोट फिर भी गिने गए): {type(e).__name__}: {e}")

    # हर विकल्प के कुल वोट सीधे टेलीग्राम से (उपयोगकर्ता-वार सूची न मिले तब भी काम आता है)
    try:
        msg = await client.get_messages(chat_id, q["message_id"])
        q["summary"] = [int(o.voter_count) for o in msg.poll.options]
    except Exception as e:
        print(f"⚠️ कुल वोट की गिनती नहीं मिली: {type(e).__name__}: {e}")
    print(f"📊 प्रश्न {q['no']}: दर्ज उपयोगकर्ता={len(q['voted'])}, टेलीग्राम गिनती={q['summary']}")


# ==================== परिणाम तालिका ====================

def build_leaderboard(session: dict, finished: bool = True) -> str:
    total = session["asked"]
    scores = session["scores"]

    # प्रश्न-वार सारांश (टेलीग्राम की कुल गिनती से)
    summary_lines = []
    any_votes = False
    for q in sorted(session["polls"].values(), key=lambda x: x["no"]):
        counts = q.get("summary")
        if not counts:
            continue
        got = sum(counts)
        right = counts[q["correct"]] if q["correct"] < len(counts) else 0
        if got:
            any_votes = True
        summary_lines.append(
            f"प्रश्न {q['no']}: कुल उत्तर `{got}` — ✅ सही `{right}`, ❌ गलत `{got - right}`"
        )
    summary_block = ("\n\n📋 **प्रश्न-वार सारांश**\n" + "\n".join(summary_lines)) if summary_lines else ""

    if not scores:
        if any_votes:
            return (
                "🏁 **प्रश्नोत्तरी समाप्त!**\n\n"
                "ℹ️ उत्तर तो मिले, पर उपयोगकर्ता-वार जानकारी टेलीग्राम से नहीं मिल सकी। "
                "कृपया बॉट को समूह में व्यवस्थापक (Admin) बनाकर पुनः प्रयास करें।"
                + summary_block
            )
        return "🏁 **प्रश्नोत्तरी समाप्त!**\n\n😶 किसी ने भी उत्तर नहीं दिया, इसलिए परिणाम तालिका खाली है।"

    rows = []
    for uid, s in scores.items():
        attempted = s["correct"] + s["wrong"]
        skipped = max(total - attempted, 0)
        rows.append((uid, s, skipped))

    # अधिक सही उत्तर वाला ऊपर; बराबरी पर कम कुल समय वाला ऊपर
    rows.sort(key=lambda r: (-r[1]["correct"], r[1]["time"]))

    rank_labels = ["🥇 प्रथम स्थान", "🥈 द्वितीय स्थान", "🥉 तृतीय स्थान"]
    title = "🏁 **प्रश्नोत्तरी समाप्त — 🏆 अंतिम परिणाम**" if finished else "📊 **अब तक का परिणाम**"
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
    text = "\n".join(lines) + summary_block
    text += "\n\n🙏 सभी प्रतिभागियों का धन्यवाद!"
    return text


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

            # सही उत्तर निर्धारित किया हुआ क्विज़ पोल
            sent = await client.send_poll(
                chat_id=chat_id,
                question=f"[{i}/{total}] {data['question']}"[:300],
                options=data["options"],
                is_anonymous=False,
                type=PollType.QUIZ,
                correct_option_id=data["correct_option_id"],
                explanation=data["explanation"][:200],
                open_period=TIMER_SECONDS
            )

            # Pyrogram में poll.id टेक्स्ट होता है, वोट अपडेट में संख्या; इसलिए int में बदलें
            poll_id = int(sent.poll.id)
            POLL_TO_CHAT[poll_id] = chat_id
            q = {
                "no": i,
                "summary": None,   # हर विकल्प के कुल वोट (Telegram से)
                "correct": data["correct_option_id"],
                "n_options": len(data["options"]),
                "message_id": sent.id,
                "sent_at": time.time(),
                "limit": TIMER_SECONDS,
                "voted": {}
            }
            session["polls"][poll_id] = q
            session["asked"] += 1

            # समय पूरा होने तक रुकें, फिर छूटे वोटों का मिलान
            await asyncio.sleep(TIMER_SECONDS + 1)
            await reconcile_votes(client, chat_id, q, session)

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
        f"📚 विषय: `{clean(topic)}`\n"
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
    data = query.data or ""

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
    """लॉग समूह में सूचना भेजना; न जाए तो मालिक को; असली कारण लॉग में छापना।"""
    targets = []
    for t in (LOG_GROUP_ID, OWNER_ID):
        if t and t not in targets:
            targets.append(t)

    if not targets:
        print("⚠️ LOG_GROUP_ID और OWNER_ID दोनों निर्धारित नहीं हैं। सूचना छोड़ दी गई।")
        return

    bot_info = await app.get_me()
    text = (
        "🚀 **प्रश्नोत्तरी बॉट सफलतापूर्वक चालू हो गया!**\n\n"
        f"🤖 **बॉट:** @{bot_info.username}\n"
        f"⏱️ **डिफ़ॉल्ट समय-सीमा:** `{TIMER_SECONDS}` सेकंड\n"
        "⚡ **स्थिति:** सक्रिय और तैयार!"
    )

    for target in targets:
        for attempt in range(1, 4):
            try:
                try:
                    await app.get_chat(target)  # चैट की जानकारी पहले से लोड करना
                except Exception as e:
                    print(f"ℹ️ get_chat({target}) विफल: {type(e).__name__}: {e}")

                await app.send_message(chat_id=target, text=text)
                print(f"✅ आरंभ सूचना भेज दी गई: {target}")
                return
            except Exception as e:
                print(f"❌ आरंभ सूचना विफल (लक्ष्य {target}, प्रयास {attempt}): {type(e).__name__}: {e}")
                await asyncio.sleep(3)

        if target == LOG_GROUP_ID:
            print(
                "💡 सुझाव: (1) LOG_GROUP_ID सही हो, समूह के लिए सामान्यतः -100 से शुरू होती है; "
                "(2) बॉट उस समूह का सदस्य हो; (3) समूह में /id भेजकर सही आईडी जाँचें।"
            )
        else:
            print("💡 सुझाव: मालिक को निजी संदेश भेजने से पहले बॉट पर एक बार /start दबाना ज़रूरी है।")


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
