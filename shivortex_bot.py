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

# =========================
# ENV VARIABLES
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_KEY = os.getenv("CF_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

if not TELEGRAM_TOKEN or not CF_ACCOUNT_ID or not CF_API_KEY or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing one or more core env vars: TELEGRAM_TOKEN, CF_ACCOUNT_ID, CF_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY")

if not ADMIN_TELEGRAM_ID:
    raise RuntimeError("ADMIN_TELEGRAM_ID env var is required (your Telegram user ID).")

ADMIN_TELEGRAM_ID = int(ADMIN_TELEGRAM_ID)

# =========================
# CLIENTS
# =========================

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

CF_MODEL = "@cf/meta/llama-3-8b-instruct"
CF_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"

HEADERS = {
    "Authorization": f"Bearer {CF_API_KEY}",
    "Content-Type": "application/json",
}

# =========================
# HELPER: ADMIN CHECK
# =========================

def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_TELEGRAM_ID

# =========================
# SYSTEM PROMPT STORAGE
# =========================
# Uses table: bot_settings(id int primary key, system_prompt text)

def get_system_prompt() -> str:
    res = supabase.table("bot_settings").select("system_prompt").eq("id", 1).execute()
    if not res.data:
        # fallback default if row missing
        return (
            "You are SHIVORTEX, a private AI assistant for Shivam. "
            "Use only information explicitly provided by Shivam. "
            "Never invent facts. Be logical, practical, and concise."
        )
    return res.data[0]["system_prompt"]

def update_system_prompt(prompt: str) -> None:
    supabase.table("bot_settings").upsert({"id": 1, "system_prompt": prompt}).execute()

# =========================
# CHAT MEMORY HELPERS
# =========================
# Uses table: messages(id, chat_id, role, content, created_at)

def save_message(chat_id: int, role: str, content: str) -> None:
    supabase.table("messages").insert({
        "chat_id": chat_id,
        "role": role,
        "content": content,
    }).execute()

def load_history(chat_id: int, limit: int = 6):
    res = (
        supabase.table("messages")
        .select("role,content")
        .eq("chat_id", chat_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed([(row["role"], row["content"]) for row in res.data]))

def load_full_history(chat_id: int):
    res = (
        supabase.table("messages")
        .select("role,content,created_at")
        .eq("chat_id", chat_id)
        .order("created_at", desc=True)
        .execute()
    )
    return list(reversed(res.data))

# =========================
# AI CALL
# =========================

def call_llm(user_input: str, history):
    system_prompt = get_system_prompt()

    messages = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_input})

    payload = {"messages": messages}

    r = requests.post(CF_URL, headers=HEADERS, json=payload, timeout=30)
    data = r.json()

    try:
        return data["result"]["response"]
    except Exception:
        # if something weird comes back, just show it
        return f"⚠️ LLM error: {data}"

# =========================
# COMMAND HANDLERS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ SHIVORTEX is online.\nJust send a message and I’ll respond.")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"👤 User: {u.full_name}\n🆔 User ID: {u.id}"
    )

async def amadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text("✅ You are recognised as ADMIN.")
    else:
        await update.message.reply_text("❌ You are NOT recognised as admin.")

async def setprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ You are not allowed to change the system prompt.")
        return

    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Usage:\n/setprompt <your rules & instructions>")
        return

    update_system_prompt(prompt)
    await update.message.reply_text("✅ System prompt updated.")

async def viewprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ You are not allowed to view the system prompt.")
        return

    await update.message.reply_text(get_system_prompt())

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear ALL messages for this chat."""
    if not is_admin(update):
        await update.message.reply_text("⛔ You are not allowed to reset chat memory.")
        return

    chat_id = update.effective_chat.id
    supabase.table("messages").delete().eq("chat_id", chat_id).execute()
    await update.message.reply_text("✅ Chat memory for this chat has been reset.")

async def export_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export full chat history as a .txt file (admin only)."""
    if not is_admin(update):
        await update.message.reply_text("⛔ You are not allowed to export chat history.")
        return

    chat_id = update.effective_chat.id
    rows = load_full_history(chat_id)

    if not rows:
        await update.message.reply_text("No history to export for this chat.")
        return

    lines = []
    lines.append(f"SHIVORTEX Chat Export\nChat ID: {chat_id}\n\n")
    for row in rows:
        role = row["role"]
        content = (row["content"] or "").strip()
        if role == "user":
            lines.append("User:\n")
        else:
            lines.append("Assistant:\n")
        lines.append(content + "\n\n")

    text_log = "".join(lines)

    filename = f"shivortex_chat_{chat_id}.txt"
    filepath = f"/tmp/{filename}"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(text_log)

    with open(filepath, "rb") as f:
        await context.bot.send_document(
            chat_id=chat_id,
            document=f,
            filename=filename,
            caption="📄 Chat history export",
        )

# =========================
# MESSAGE HANDLER
# =========================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    history = load_history(chat_id)
    reply = call_llm(text, history)

    save_message(chat_id, "user", text)
    save_message(chat_id, "assistant", reply)

    await update.message.reply_text(reply)

# =========================
# MAIN
# =========================

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("amadmin", amadmin))
    app.add_handler(CommandHandler("setprompt", setprompt))
    app.add_handler(CommandHandler("viewprompt", viewprompt))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("export", export_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ SHIVORTEX running")
    app.run_polling()

if __name__ == "__main__":
    main()

