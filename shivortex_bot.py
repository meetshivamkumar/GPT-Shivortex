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

# ======================
# ENV VARS
# ======================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_KEY = os.getenv("CF_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID"))

# ======================
# CLIENTS
# ======================

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

CF_MODEL = "@cf/meta/llama-3-8b-instruct"
CF_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"

HEADERS = {
    "Authorization": f"Bearer {CF_API_KEY}",
    "Content-Type": "application/json",
}

# ======================
# PROMPT MANAGEMENT
# ======================

def get_system_prompt():
    res = supabase.table("bot_settings").select("system_prompt").eq("id", 1).execute()
    return res.data[0]["system_prompt"]

def update_system_prompt(prompt: str):
    supabase.table("bot_settings").update(
        {"system_prompt": prompt}
    ).eq("id", 1).execute()

# ======================
# CHAT MEMORY
# ======================

def save_message(chat_id, role, content):
    supabase.table("messages").insert({
        "chat_id": chat_id,
        "role": role,
        "content": content
    }).execute()

def load_history(chat_id, limit=6):
    res = (
        supabase.table("messages")
        .select("role,content")
        .eq("chat_id", chat_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed([(r["role"], r["content"]) for r in res.data]))

# ======================
# LLM CALL
# ======================

def call_llm(user_input, history):
    system_prompt = get_system_prompt()

    messages = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_input})

    payload = {"messages": messages}
    r = requests.post(CF_URL, headers=HEADERS, json=payload, timeout=30)
    data = r.json()

    return data["result"]["response"]

# ======================
# COMMANDS
# ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ SHIVORTEX online.")

async def setprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Usage: /setprompt <text>")
        return

    update_system_prompt(prompt)
    await update.message.reply_text("✅ System prompt updated.")

async def viewprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    await update.message.reply_text(get_system_prompt())

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"User: {u.full_name}\nUser ID: {u.id}"
    )

# ======================
# MESSAGE HANDLER
# ======================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    history = load_history(chat_id)
    reply = call_llm(text, history)

    save_message(chat_id, "user", text)
    save_message(chat_id, "assistant", reply)

    await update.message.reply_text(reply)

# ======================
# MAIN
# ======================

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("setprompt", setprompt))
    app.add_handler(CommandHandler("viewprompt", viewprompt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ SHIVORTEX running")
    app.run_polling()

if __name__ == "__main__":
    main()
