import os
import re
import json
import base64
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

# फोटो से प्रश्न बनाने वाला (vision) मॉडल; बदलना हो तो env में GROQ_VISION_MODELS रखें
GROQ_VISION_MODELS = [
    m.strip()
    for m in os.getenv("GROQ_VISION_MODELS", "qwen/qwen3.8-27b").split(",")
    if m.strip()
]

# जिन समूहों में फोटो-से-प्रश्न बंद किया गया है
PHOTO_QUIZ_OFF = set()

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


# ==================== फोटो से प्रश्न ====================

def extract_json(text: str) -> dict:
    """मॉडल के उत्तर से JSON निकालना (think-ब्लॉक और कोड-बाड़ हटाकर)।"""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = text.replace("```json", "").replace("```", "")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("उत्तर में JSON नहीं मिला")
    return json.loads(text[start:end + 1])


async def generate_quiz_from_image(image_bytes: bytes, hint: str = "") -> dict:
    """प्रश्न की फोटो पढ़कर 4 विकल्पों वाला MCQ बनाना।"""
    b64 = base64.b64encode(image_bytes).decode()
    hint_text = f'\n    Extra note from the user about this image: "{hint}"' if hint else ""

    prompt = f"""
    You are an expert exam setter for Indian competitive exams. The image is a photo of an exam question. It may be in Hindi, English or mixed, and it may or may not already contain answer options.

    Tasks:
    1. Read the question carefully from the image.
    2. If the image already shows options, use them (maximum 4). If it shows no options, create 4 plausible, distinct options.
    3. Work out the correct answer yourself using your knowledge. Do NOT blindly trust any answer marked in the image.
    4. If the image has no readable, self-contained question (for example it is not a question, or it depends on a figure that cannot be expressed in text), return only {{"is_question": false}}.
    5. LANGUAGE: Write the question, options and explanation in pure, standard Hindi using Devanagari script (शुद्ध हिंदी). Translate English text into Hindi, but keep official abbreviations, numbers, names and formulas as they are.
    6. Keep the question under 220 characters, each option under 90 characters, explanation under 180 characters.
    7. Return RAW JSON ONLY without markdown backticks.{hint_text}

    Required JSON Schema:
    {{
      "is_question": true,
      "question": "प्रश्न यहाँ",
      "options": ["विकल्प क", "विकल्प ख", "विकल्प ग", "विकल्प घ"],
      "correct_option_id": 0,
      "explanation": "1-2 पंक्तियों में स्पष्ट व्याख्या"
    }}
    """

    errors = []
    for model_name in GROQ_VISION_MODELS:
        try:
            completion = await groq_client.chat.completions.create(
                model=model_name,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                    ]
                }],
                temperature=0.2,
                max_completion_tokens=2500,
                response_format={"type": "json_object"}
            )
            data = extract_json(completion.choices[0].message.content)

            if not data.get("is_question", True):
                return {"is_question": False}

            options = [str(o).strip()[:100] for o in data["options"][:4]]
            cid = int(data["correct_option_id"])
            if len(options) < 2 or not (0 <= cid < len(options)):
                raise ValueError("मॉडल से अमान्य प्रश्न मिला")

            return {
                "is_question": True,
                "question": str(data["question"]).strip(),
                "options": options,
                "correct_option_id": cid,
                "explanation": str(data.get("explanation", "")).strip()
            }
        except Exception as e:
            print(f"⚠️ विज़न मॉडल {model_name} विफल: {type(e).__name__}: {e}")
            errors.append(f"{model_name}: {str(e)[:120]}")

    raise RuntimeError(" | ".join(errors))


# ==================== वोट दर्ज करना ====================

def record_vote(session: dict, q: dict, uid: int, name: str, chosen: int, elapsed: float):
    """एक उपयोगकर्ता का एक प्रश्न का उत्तर दर्ज करना (दोहराव से बचाव सहित)।"""
    if uid in q["voted"]:
        return
    is_right = (chosen == q["correct"])
    q["voted"][uid] = is_right
    q.setdefault("times", {})[uid] = max(elapsed, 0.0)

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
        line = f"प्रश्न {q['no']}: कुल उत्तर `{got}` — ✅ सही `{right}`, ❌ गलत `{got - right}`"

        # इस प्रश्न का सबसे पहले सही उत्तर देने वाला
        times = q.get("times", {})
        correct_users = [(times.get(u, 9999), u) for u, ok in q["voted"].items() if ok]
        if correct_users:
            _, first_uid = min(correct_users)
            first_name = session["scores"].get(first_uid, {}).get("name", f"उपयोगकर्ता {first_uid}")
            line += f"\n    ⚡ सबसे पहले सही: {first_name}"
        summary_lines.append(line)
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
            f"    📝 उत्तर दिए: `{s['correct'] + s['wrong']}/{total}` प्रश्न\n"
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


async def _return_preset(data: dict) -> dict:
    return data


async def run_quiz(client: Client, chat_id: int, topic: str, total: int, preset: dict = None):
    session = SESSIONS[chat_id]
    asked_questions = []
    next_task = None

    try:
        if preset:
            # फोटो से बना प्रश्न पहले से तैयार है
            next_task = asyncio.create_task(_return_preset(preset))
        else:
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
        "📷 **फोटो से प्रश्न:** स्वामी किसी प्रश्न की फोटो समूह में डालें, "
        "बॉट खुद 4 विकल्पों वाला प्रश्न बनाकर पूछेगा। (बंद/चालू: `/photoquiz on|off`)\n\n"
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
        await message.reply_text("⛔ **यह आदेश केवल स्वामी (Owner) के लिए है!**")
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


@app.on_message(filters.photo & filters.group)
async def photo_quiz_handler(client: Client, message: Message):
    """स्वामी प्रश्न की फोटो डालें तो बॉट MCQ बनाकर पूछे।"""
    if not message.from_user:
        return
    chat_id = message.chat.id
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        return
    if chat_id in PHOTO_QUIZ_OFF:
        return
    # स्वामी न हो तो चुप रहें, ताकि हर सदस्य की फोटो पर बॉट न बोले
    if not await is_owner(client, chat_id, message.from_user.id):
        return

    if chat_id in SESSIONS:
        await message.reply_text("⚠️ अभी प्रश्नोत्तरी चल रही है। उसके समाप्त होने के बाद फोटो भेजें।")
        return

    status = await message.reply_text("📷 फोटो पढ़कर प्रश्न बना रहे हैं, कृपया प्रतीक्षा करें...")

    try:
        buf = await client.download_media(message, in_memory=True)
        if buf is None:
            raise RuntimeError("फोटो डाउनलोड नहीं हो सकी")
        image_bytes = buf.getvalue()

        hint = (message.caption or "").strip()[:200]
        data = await generate_quiz_from_image(image_bytes, hint)
    except Exception as e:
        print(f"❌ फोटो से प्रश्न बनाने में त्रुटि: {e}")
        await status.edit_text(f"❌ फोटो से प्रश्न नहीं बन सका।\n`{clean(str(e))[:250]}`")
        return

    if not data.get("is_question"):
        await status.edit_text("⚠️ इस फोटो में कोई स्पष्ट प्रश्न नहीं मिला। कृपया साफ़ और पूरे प्रश्न की फोटो भेजें।")
        await asyncio.sleep(6)
        try:
            await status.delete()
        except Exception:
            pass
        return

    # प्रश्न मिल गया: सत्र बनाकर प्रश्नोत्तरी चलाना
    SESSIONS[chat_id] = {"scores": {}, "polls": {}, "asked": 0, "task": None}
    await status.edit_text("✅ फोटो से प्रश्न तैयार! नीचे दिए प्रश्न का उत्तर दें।")
    SESSIONS[chat_id]["task"] = asyncio.create_task(
        run_quiz(client, chat_id, "फोटो से बना प्रश्न", 1, preset=data)
    )


@app.on_message(filters.command("photoquiz") & filters.group)
async def photoquiz_toggle_handler(client: Client, message: Message):
    if not message.from_user:
        return
    if not await is_owner(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **यह आदेश केवल स्वामी के लिए है!**")
        return

    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg == "on":
        PHOTO_QUIZ_OFF.discard(message.chat.id)
        await message.reply_text("✅ **फोटो से प्रश्न** चालू कर दिया गया।")
    elif arg == "off":
        PHOTO_QUIZ_OFF.add(message.chat.id)
        await message.reply_text("🛑 **फोटो से प्रश्न** बंद कर दिया गया।")
    else:
        state = "बंद 🛑" if message.chat.id in PHOTO_QUIZ_OFF else "चालू ✅"
        await message.reply_text(
            f"📷 **फोटो से प्रश्न:** {state}\n\nबदलने के लिए `/photoquiz on` या `/photoquiz off` लिखें।"
        )


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
            BotCommand("photoquiz", "फोटो से प्रश्न चालू/बंद करें"),
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
