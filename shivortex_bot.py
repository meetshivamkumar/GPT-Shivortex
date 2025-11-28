import os
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
from supabase import create_client

# =============================
# ENV VARIABLES (REQUIRED)
# =============================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_KEY = os.getenv("CF_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

missing = [
    name for name, val in {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "CF_ACCOUNT_ID": CF_ACCOUNT_ID,
        "CF_API_KEY": CF_API_KEY,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
    }.items() if not val
]

if missing:
    raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

# =============================
# SUPABASE SETUP
# =============================

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# =============================
# CLOUDFLARE AI CONFIG
# =============================

CF_MODEL = "@cf/meta/llama-3-8b-instruct"
CF_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"

CF_HEADERS = {
    "Authorization": f"Bearer {CF_API_KEY}",
    "Content-Type": "application/json",
}

SYSTEM_PROMPT = (
    "You are SHIVORTEX, a private AI assistant for Shivam.\n"
    "Be concise, logical, and practical.\n"
    "Reply ONCE per message.\n"
)

# =============================
# DATABASE HELPERS
# =============================

def ensure_chat(chat_id: int):
    supabase.table("chats").upsert({"chat_id": chat_id}).execute()

def save_message(chat_id: int, role: str, content: str):
    supabase.table("messages").insert({
        "chat_id": chat_id,
        "role": role,
        "content": content
    }).execute()

def load_history(chat_id: int, limit=6):
    res = (
        supabase.table("messages")
        .select("role,content")
        .eq("chat_id", chat_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed([(r["role"], r["content"]) for r in res.data]))

# =============================
# CLOUDFLARE LLM CALL
# =============================

def call_llm(user_message: str, history: list):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for role, content in history:
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_message})

    payload = {"messages": messages}

    r = requests.post(CF_URL, headers=CF_HEADERS, json=payload, timeout=30)
    data = r.json()

    try:
        return data["result"]["response"]
    except Exception:
        return f"⚠️ LLM error: {data}"

# =============================
# TELEGRAM HANDLERS
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ SHIVORTEX is online.\n\n"
        "Just send a message and I’ll respond."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    ensure_chat(chat_id)

    history = load_history(chat_id)
    reply = call_llm(text, history)

    save_message(chat_id, "user", text)
    save_message(chat_id, "assistant", reply)

    await update.message.reply_text(reply)

# =============================
# MAIN APP
# =============================

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Shivortex Telegram bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
