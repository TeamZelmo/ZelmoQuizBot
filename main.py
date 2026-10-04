import os
import json
from google import genai
from google.genai import types
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import Message

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

TIMER_SECONDS = int(os.getenv("TIMER_SECONDS", "30"))

app = Client(
    "competition_ca_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

ai_client = genai.Client(api_key=GEMINI_API_KEY)


async def is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False


def generate_exam_ca_quiz(category: str) -> dict:
    prompt = f"""
    Create 1 high-yield, factual Multiple Choice Question (MCQ) for competitive exams (UPSC/SSC/State PCS/Banking).
    Target Topic/Domain: {category}.
    Focus: Recent government schemes, summits, military exercises, indices, appointments, or awards from verified current news.
    Language: Bilingual/Hinglish.

    Rules:
    - Avoid gossip news; stick to syllabus-relevant events.
    - Exactly 4 options.
    - Factual 1-2 line explanation.
    - Raw JSON only without markdown fences.

    Schema:
    {{
      "question": "Question statement here",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct_option_id": 0,
      "explanation": "Key factual detail for exam revision"
    }}
    """

    response = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2
        )
    )

    clean_text = response.text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)


@app.on_message(filters.command("ca") & filters.group)
async def exam_quiz_handler(client: Client, message: Message):
    if not await is_admin(client, message.chat.id, message.from_user.id):
        await message.reply_text("⛔ **Sirf Group Admins hi quiz start kar sakte hain!**")
        return

    category = "Government Schemes, Summits, Defense and Economy"
    if len(message.command) > 1:
        category = " ".join(message.command[1:])

    status_msg = await message.reply_text(f"🎯 **[Admin Initiated]** `{category}` par question generate ho raha hai...")

    try:
        data = generate_exam_ca_quiz(category)
        options = data["options"][:4]

        await client.send_poll(
            chat_id=message.chat.id,
            question=f"⏱️ [Timer: {TIMER_SECONDS}s]\n" + data["question"],
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


if __name__ == "__main__":
    print("Admin CA Bot Render par start ho raha hai...")
    app.run()
