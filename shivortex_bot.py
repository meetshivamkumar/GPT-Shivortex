# shivortex_bot.py
# Updated for speed + completeness (Cloudflare + Supabase + Telegram)
import os
import time
import traceback
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
# ENV
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_KEY = os.getenv("CF_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

if not all([TELEGRAM_TOKEN, CF_ACCOUNT_ID, CF_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ADMIN_TELEGRAM_ID]):
    raise RuntimeError("Missing env vars. Required: TELEGRAM_TOKEN, CF_ACCOUNT_ID, CF_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ADMIN_TELEGRAM_ID")

ADMIN_TELEGRAM_ID = int(ADMIN_TELEGRAM_ID)

# ======================
# Clients & config
# ======================
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Default: faster smaller model (3b). Swap to 8b if you prefer quality.
CF_MODEL = "@cf/meta/llama-3-3b-instruct"
CF_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
CF_HEADERS = {"Authorization": f"Bearer {CF_API_KEY}", "Content-Type": "application/json"}

# Enforced rules and defaults
ENFORCED_RULES = (
    "ENFORCED RULES:\n"
    "1) Use ONLY facts provided by the user or stored memory; do NOT invent data.\n"
    "2) Reply once per user message; do NOT produce multi-turn transcripts.\n"
    "3) Do not apologize or reference earlier assistant replies unless explicitly asked.\n"
)

DEFAULT_STYLE = "brief"
DEFAULT_MAX_WORDS = 140    # used as a soft limit

# ========== Supabase bot_settings helpers ==========
def get_bot_settings():
    res = supabase.table("bot_settings").select("system_prompt,style,max_tokens").eq("id", 1).execute()
    if res.data and len(res.data) > 0:
        row = res.data[0]
        return {
            "system_prompt": row.get("system_prompt") or "",
            "style": row.get("style") or DEFAULT_STYLE,
            "max_tokens": row.get("max_tokens") or DEFAULT_MAX_WORDS,
        }
    # upsert defaults
    supabase.table("bot_settings").upsert({
        "id": 1,
        "system_prompt": "You are SHIVORTEX, a private AI assistant for Shivam. Use only user-provided facts. Be concise and practical.",
        "style": DEFAULT_STYLE,
        "max_tokens": DEFAULT_MAX_WORDS
    }).execute()
    return {"system_prompt": "You are SHIVORTEX, a private AI assistant for Shivam. Use only user-provided facts. Be concise and practical.", "style": DEFAULT_STYLE, "max_tokens": DEFAULT_MAX_WORDS}

def admin_update_prompt(new_prompt: str):
    settings = get_bot_settings()
    supabase.table("bot_settings").upsert({"id": 1, "system_prompt": new_prompt, "style": settings["style"], "max_tokens": settings["max_tokens"]}).execute()

def admin_set_style(style: str):
    settings = get_bot_settings()
    supabase.table("bot_settings").upsert({"id": 1, "system_prompt": settings["system_prompt"], "style": style, "max_tokens": settings["max_tokens"]}).execute()

def admin_set_max_tokens(n: int):
    settings = get_bot_settings()
    supabase.table("bot_settings").upsert({"id": 1, "system_prompt": settings["system_prompt"], "style": settings["style"], "max_tokens": n}).execute()

# ========== Memory helpers ==========
def save_message(chat_id: int, role: str, content: str) -> None:
    supabase.table("messages").insert({"chat_id": chat_id, "role": role, "content": content}).execute()

def load_history(chat_id: int, limit: int = 4):
    """Smaller window by default — reduces tokens and speeds up responses."""
    res = supabase.table("messages").select("role,content").eq("chat_id", chat_id).order("created_at", desc=True).limit(limit).execute()
    return list(reversed([(r["role"], r["content"]) for r in (res.data or [])]))

def load_full_history(chat_id: int):
    res = supabase.table("messages").select("role,content,created_at").eq("chat_id", chat_id).order("created_at", desc=True).execute()
    return list(reversed(res.data or []))

# ========== Prompt building ==========
def build_prompt_for_model(chat_id: int, user_message: str, history_limit: int = 4) -> str:
    settings = get_bot_settings()
    system_prompt = settings["system_prompt"].strip()
    style = settings["style"]
    max_tokens = settings["max_tokens"]

    style_instruction = ("Style: brief, 1-2 sentences." if style == "brief" else "Style: detailed, clear bullet points with steps.")

    system_block = (
        f"{system_prompt}\n\n"
        f"{ENFORCED_RULES}\n"
        f"{style_instruction}\n"
        f"MAX_APPROX_WORDS: {max_tokens}\n\n"
    )

    history = load_history(chat_id, limit=history_limit)
    history_text = ""
    for role, content in history:
        role_label = "User" if role == "user" else "Assistant"
        # compress history: keep only first 120 chars of each content
        short = content if len(content) <= 240 else content[:240] + "..."
        history_text += f"{role_label}: {short}\n"

    prompt = system_block + (("History:\n" + history_text + "\n") if history_text else "") + f"User: {user_message}\nAssistant:"
    return prompt

# ========== Cloudflare call with params & quick-regeneration fallback ==========
def call_cloudflare_model(chat_id: int, user_message: str, attempts: int = 2) -> str:
    """
    Calls Cloudflare Workers AI with explicit parameters to ensure a full answer.
    Uses a small number of attempts; history window is small for speed.
    """
    settings = get_bot_settings()
    max_words = settings["max_tokens"]

    # Cloudflare parameter mapping: use max_output_tokens ~ max_words * 2 (approx)
    # (Cloudflare params may be model-specific; this generic parameter is commonly supported)
    params = {
        "max_output_tokens": max(120, min(1024, int(max_words * 3))),  # allow more tokens for detailed responses
        "temperature": 0.25,
        "top_p": 0.9,
        "repetition_penalty": 1.02
    }

    last_response = None
    for attempt in range(attempts):
        prompt = build_prompt_for_model(chat_id, user_message)
        # Cloudflare accepts a messages array; also allow "options" / "parameters" depending on account.
        payload = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_message}
            ],
            "parameters": params
        }

        try:
            r = requests.post(CF_URL, headers=CF_HEADERS, json=payload, timeout=45)
            data = r.json()
        except Exception as e:
            last_response = f"⚠️ LLM network error: {e}"
            # quick retry once if network fluked
            if attempt == attempts - 1:
                return last_response
            time.sleep(0.6)
            continue

        # Parse response robustly
        resp_text = None
        if isinstance(data, dict):
            if "result" in data and isinstance(data["result"], dict) and "response" in data["result"]:
                resp_text = data["result"]["response"]
            elif "generated_text" in data:  # fallback
                resp_text = data.get("generated_text")
            elif "error" in data:
                last_response = f"❌ Model error: {data.get('error')}"
                # don't retry many times for model errors
                return last_response
            else:
                # unknown shape: convert to string
                resp_text = str(data)
        else:
            resp_text = str(data)

        if not isinstance(resp_text, str):
            resp_text = str(resp_text)

        resp_text = resp_text.strip()
        last_response = resp_text

        # Heuristic: if response is clearly truncated (ends with incomplete sentence or '...'),
        # or contains banned patterns like "I apologize" due to prior memory conflict, do one short retry.
        low = resp_text.lower()
        truncated = resp_text.endswith("...") or resp_text.endswith("..") or len(resp_text) < 10
        bad_phrases = ["i apologize", "as i said", "previously", "as mentioned earlier", "i'm a chatbot"]
        bad = any(p in low for p in bad_phrases)

        if not truncated and not bad:
            return resp_text

        # If truncated/bad and we can retry, make a tighter instruction
        if attempt < attempts - 1:
            stricter_suffix = ("\n\nSTRICT: Do NOT apologize or refer to prior replies. Answer concisely and directly. "
                               "If previous conversation conflicts with the current system instructions, obey the system instructions.")
            payload = {
                "messages": [
                    {"role": "system", "content": build_prompt_for_model(chat_id, user_message) + stricter_suffix},
                    {"role": "user", "content": user_message}
                ],
                "parameters": params
            }
            try:
                r2 = requests.post(CF_URL, headers=CF_HEADERS, json=payload, timeout=40)
                data2 = r2.json()
                if isinstance(data2, dict) and "result" in data2 and isinstance(data2["result"], dict) and "response" in data2["result"]:
                    resp2 = data2["result"]["response"].strip()
                else:
                    resp2 = str(data2).strip()
            except Exception as e:
                resp2 = f"⚠️ LLM retry failed: {e}"
            last_response = resp2
            # final return if no more attempts or continue loop
        time.sleep(0.4)

    return last_response or "⚠️ No response from model."

# ========== Admin helpers & commands ==========
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_TELEGRAM_ID

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ SHIVORTEX online. Use normal chat or admin commands if you are the owner.")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"User: {u.full_name}\nUser ID: {u.id}")

async def amadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text("✅ You are recognised as ADMIN.")
    else:
        await update.message.reply_text("❌ You are NOT recognised as admin.")

async def setprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not allowed.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /setprompt <text>")
        return
    admin_update_prompt(text)
    await update.message.reply_text("✅ System prompt updated.")

async def viewprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not allowed.")
        return
    settings = get_bot_settings()
    await update.message.reply_text(f"SYSTEM PROMPT:\n{settings['system_prompt']}\n\nSTYLE: {settings['style']}\nMAX_WORDS: {settings['max_tokens']}")

async def setstyle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not allowed.")
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg not in ("brief", "detailed"):
        await update.message.reply_text("Usage: /setstyle brief|detailed")
        return
    admin_set_style(arg)
    await update.message.reply_text(f"✅ Style set to {arg}.")

async def setmax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not allowed.")
        return
    try:
        n = int(context.args[0])
        admin_set_max_tokens(n)
        await update.message.reply_text(f"✅ max words set to {n}.")
    except Exception:
        await update.message.reply_text("Usage: /setmax <n>")

async def promptpreview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not allowed.")
        return
    chat_id = update.effective_chat.id
    sample = "Hello, remind me briefly what you know about my agency."
    prompt = build_prompt_for_model(chat_id, sample)
    await update.message.reply_text(f"=== PROMPT PREVIEW ===\n{prompt[:4000]}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not allowed.")
        return
    chat_id = update.effective_chat.id
    supabase.table("messages").delete().eq("chat_id", chat_id).execute()
    await update.message.reply_text("✅ Chat memory reset.")

async def export_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not allowed.")
        return
    chat_id = update.effective_chat.id
    rows = load_full_history(chat_id)
    if not rows:
        await update.message.reply_text("No history to export.")
        return
    lines = [f"SHIVORTEX Chat Export\nChat ID: {chat_id}\n\n"]
    for r in rows:
        role = r["role"]
        content = (r["content"] or "").strip()
        lines.append(("User:\n" if role == "user" else "Assistant:\n") + content + "\n\n")
    txt = "".join(lines)
    filename = f"shivortex_{chat_id}.txt"
    path = f"/tmp/{filename}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    with open(path, "rb") as f:
        await context.bot.send_document(chat_id=chat_id, document=f, filename=filename, caption="Chat export")

# ========== Message handler ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return
    history = load_history(chat_id)
    reply = call_cloudflare_model(chat_id, text)
    save_message(chat_id, "user", text)
    save_message(chat_id, "assistant", reply)
    await update.message.reply_text(reply)

# ========== Main (auto-reconnect) ==========
def main():
    while True:
        try:
            app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
            app.add_handler(CommandHandler("start", start))
            app.add_handler(CommandHandler("whoami", whoami))
            app.add_handler(CommandHandler("amadmin", amadmin))
            app.add_handler(CommandHandler("setprompt", setprompt))
            app.add_handler(CommandHandler("viewprompt", viewprompt))
            app.add_handler(CommandHandler("setstyle", setstyle))
            app.add_handler(CommandHandler("setmax", setmax))
            app.add_handler(CommandHandler("promptpreview", promptpreview))
            app.add_handler(CommandHandler("reset", reset))
            app.add_handler(CommandHandler("export", export_history))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            print("✅ SHIVORTEX running (polling)...")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print("❌ Bot crashed:", e)
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    main()
