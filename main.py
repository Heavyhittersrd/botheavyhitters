import os
import logging
import requests as req
import sqlite3
import secrets
import string
from datetime import datetime, timedelta
from flask import Flask, request, Response, jsonify
from signalwire.rest import Client as SignalWireClient
from signalwire.voice_response import VoiceResponse, Gather
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
SW_PROJECT_ID    = os.environ["SW_PROJECT_ID"]
SW_AUTH_TOKEN    = os.environ["SW_AUTH_TOKEN"]
SW_SPACE_URL     = os.environ["SW_SPACE_URL"]
SW_NUMBER        = os.environ["SW_NUMBER"]
WEBHOOK_BASE_URL = os.environ["WEBHOOK_BASE_URL"]
ADMIN_CHAT_ID    = int(os.environ.get("ADMIN_CHAT_ID", "0"))

sw_client = SignalWireClient(SW_PROJECT_ID, SW_AUTH_TOKEN, signalwire_space_url=SW_SPACE_URL)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
call_sessions: dict = {}

# ─── BASE DE DATOS ─────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("/app/bot.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS keys (
        key TEXT PRIMARY KEY, plan TEXT, days INTEGER, created_at TEXT,
        redeemed_by INTEGER DEFAULT NULL, redeemed_at TEXT DEFAULT NULL, expires_at TEXT DEFAULT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY, username TEXT, plan TEXT, expires_at TEXT, active INTEGER DEFAULT 0)""")
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect("/app/bot.db")
    conn.row_factory = sqlite3.Row
    return conn

def is_user_active(chat_id: int) -> bool:
    if chat_id == ADMIN_CHAT_ID:
        return True
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM users WHERE chat_id = ? AND active = 1", (chat_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return datetime.now() < datetime.fromisoformat(row["expires_at"])

def generate_key(plan: str, days: int) -> str:
    chars = string.ascii_uppercase + string.digits
    r = ''.join(secrets.choice(chars) for _ in range(12))
    key = f"HVY-{r[:4]}-{r[4:8]}-{r[8:12]}"
    conn = get_db()
    conn.cursor().execute("INSERT INTO keys (key, plan, days, created_at) VALUES (?, ?, ?, ?)",
        (key, plan, days, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return key

def redeem_key(key: str, chat_id: int, username: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM keys WHERE key = ?", (key,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None, "❌ Key inválida."
    if row["redeemed_by"]:
        conn.close()
        return None, "❌ Esta key ya fue usada."
    expires_at = (datetime.now() + timedelta(days=row["days"])).isoformat()
    c.execute("UPDATE keys SET redeemed_by=?, redeemed_at=?, expires_at=? WHERE key=?",
        (chat_id, datetime.now().isoformat(), expires_at, key))
    c.execute("""INSERT INTO users (chat_id, username, plan, expires_at, active) VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET plan=?, expires_at=?, active=1""",
        (chat_id, username, row["plan"], expires_at, row["plan"], expires_at))
    conn.commit()
    conn.close()
    return {"plan": row["plan"], "days": row["days"], "expires_at": expires_at}, None

# ─── IVR ──────────────────────────────────────────────────────────────────────
IVR_MENSAJES = {
    "cobrar": "Hola, le llamamos de parte de nuestra empresa. Usted tiene una factura pendiente. Si desea pagar ahora marque 1. Para hablar con un agente marque 2. Para escuchar el monto marque 3.",
    "confirmar": "Hola, le llamamos para confirmar su cita. Si confirma marque 1. Para cancelar marque 2. Para reprogramar marque 3.",
    "recordatorio": "Hola, tiene un pago proximo a vencer. Si ya pago marque 1. Si necesita mas tiempo marque 2. Para hablar con un agente marque 3.",
    "encuesta": "Hola, le llamamos para una encuesta de satisfaccion. Si desea participar marque 1. Si no desea participar marque 2.",
}
IVR_OPCIONES = {
    "cobrar":      {"1": "💳 Quiere PAGAR", "2": "👤 Quiere agente", "3": "🔊 Quiere escuchar monto"},
    "confirmar":   {"1": "✅ CONFIRMÓ cita", "2": "❌ CANCELÓ cita", "3": "🔄 Quiere reprogramar"},
    "recordatorio":{"1": "✅ Ya pagó", "2": "⏳ Necesita más tiempo", "3": "👤 Quiere agente"},
    "encuesta":    {"1": "✅ Acepta encuesta", "2": "❌ Rechaza encuesta"},
}
IVR_RESPUESTA = {
    "cobrar":      {"1": "Perfecto. Recibira un enlace de pago pronto.", "2": "Le comunicamos con un agente.", "3": "Su saldo esta disponible en linea."},
    "confirmar":   {"1": "Su cita esta confirmada.", "2": "Su cita ha sido cancelada.", "3": "Le contactaremos para reprogramar."},
    "recordatorio":{"1": "Hemos registrado su pago.", "2": "Un agente le contactara pronto.", "3": "Le transferimos con un agente."},
    "encuesta":    {"1": "Gracias por participar.", "2": "Que tenga un buen dia."},
}

# ─── FLASK + WEBHOOK ──────────────────────────────────────────────────────────
flask_app = Flask(__name__)
telegram_app = None

def notify_telegram(chat_id: int, texto: str):
    try:
        req.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram notify error: {e}")

@flask_app.route(f"/telegram/{TELEGRAM_TOKEN}", methods=["POST"])
async def telegram_webhook():
    data = request.get_json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return jsonify({"ok": True})

@flask_app.route("/voice/<action>", methods=["POST"])
def voice_webhook(action):
    response = VoiceResponse()
    gather = Gather(num_digits=1, action=f"{WEBHOOK_BASE_URL}/gather/{action}", method="POST", timeout=10)
    gather.say(IVR_MENSAJES.get(action, "Marque 1 o 2."), language="es-MX")
    response.append(gather)
    response.say("No recibimos respuesta. Hasta luego.", language="es-MX")
    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/gather/<action>", methods=["POST"])
def gather_webhook(action):
    digit = request.form.get("Digits", "?")
    call_sid = request.form.get("CallSid", "")
    to_number = request.form.get("To", "")
    session = call_sessions.get(call_sid, {})
    chat_id = session.get("chat_id", ADMIN_CHAT_ID)
    texto_opcion = IVR_OPCIONES.get(action, {}).get(digit, f"Marcó: {digit}")
    notify_telegram(chat_id, f"📞 *Respuesta*\n📱 `{to_number}`\n🎯 `{action.upper()}`\n🔢 *{digit}*\n📋 {texto_opcion}")
    response = VoiceResponse()
    response.say(IVR_RESPUESTA.get(action, {}).get(digit, "Gracias. Que tenga un buen dia."), language="es-MX")
    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/status", methods=["POST"])
def call_status():
    status = request.form.get("CallStatus", "")
    to_number = request.form.get("To", "")
    call_sid = request.form.get("CallSid", "")
    session = call_sessions.get(call_sid, {})
    chat_id = session.get("chat_id", ADMIN_CHAT_ID)
    iconos = {"no-answer": "📵 No contestó", "busy": "📶 Ocupado", "failed": "❌ Falló", "canceled": "🚫 Cancelado"}
    if status in iconos:
        notify_telegram(chat_id, f"📞 *Estado*\n📱 `{to_number}`\n{iconos[status]}")
    return "", 204

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

# ─── TECLADO ──────────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📞 Cobrar"), KeyboardButton("📅 Confirmar")],
        [KeyboardButton("🔔 Recordatorio"), KeyboardButton("📊 Encuesta")],
        [KeyboardButton("🔑 Redeem Key"), KeyboardButton("ℹ️ Mi Plan")],
        [KeyboardButton("🛒 Comprar Plan"), KeyboardButton("📞 Soporte")],
    ], resize_keyboard=True, persistent=True)

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_user_active(chat_id):
        await update.message.reply_text("👋 *Bienvenido!*\nSelecciona una acción:", parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        keyboard = [[InlineKeyboardButton("🛒 Ver Planes", callback_data="ver_planes"),
                     InlineKeyboardButton("🔑 Tengo una Key", callback_data="tengo_key")]]
        await update.message.reply_text(
            "👋 *Bienvenido al Bot OTP de HeavyHitters!*\n\n📞 Llama a tus clientes automáticamente.\n\nActiva tu plan para comenzar:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Sin permiso.")
        return
    planes = {"1dia": (1, "1 Día", "$30"), "3dias": (3, "3 Días", "$70"), "semana": (7, "1 Semana", "$100"), "mes": (30, "1 Mes", "$300")}
    if not context.args or context.args[0].lower() not in planes:
        await update.message.reply_text("⚠️ Uso:\n`/genkey 1dia`\n`/genkey 3dias`\n`/genkey semana`\n`/genkey mes`", parse_mode="Markdown")
        return
    days, plan_name, precio = planes[context.args[0].lower()]
    key = generate_key(plan_name, days)
    await update.message.reply_text(
        f"✅ *Key Generada*\n━━━━━━━━━━━━━\n🔑 `{key}`\n📋 Plan: *{plan_name}*\n💰 Precio: *{precio}*\n\nEnvíale esta key al cliente.",
        parse_mode="Markdown")

async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    username = update.effective_user.username or str(chat_id)
    if not context.args:
        await update.message.reply_text("⚠️ Uso: `/redeem HVY-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return
    result, error = redeem_key(context.args[0].upper().strip(), chat_id, username)
    if error:
        await update.message.reply_text(error)
        return
    expires = datetime.fromisoformat(result["expires_at"])
    await update.message.reply_text(
        f"🎉 *¡Activado!*\n📋 Plan: *{result['plan']}*\n📅 Expira: *{expires.strftime('%d/%m/%Y')}*",
        parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_miplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_CHAT_ID:
        await update.message.reply_text("👑 Admin — acceso ilimitado.", reply_markup=main_keyboard())
        return
    conn = get_db()
    row = conn.cursor().execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ Sin plan activo. Usa /redeem para activar.")
        return
    expires = datetime.fromisoformat(row["expires_at"])
    restante = expires - datetime.now()
    if restante.total_seconds() < 0:
        await update.message.reply_text("⚠️ Tu plan *expiró*. Contacta soporte.", parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"📋 *Tu Plan*\n🎯 {row['plan']}\n📅 Expira: {expires.strftime('%d/%m/%Y')}\n⏳ {restante.days}d {restante.seconds//3600}h restantes\n✅ Activo",
        parse_mode="Markdown")

async def hacer_llamada(update: Update, chat_id: int, numero: str, accion: str):
    try:
        await update.message.reply_text(f"📞 Llamando a `{numero}`\n🎯 *{accion.upper()}*...", parse_mode="Markdown")
        call = sw_client.calls.create(
            to=numero, from_=SW_NUMBER,
            url=f"{WEBHOOK_BASE_URL}/voice/{accion}",
            status_callback=f"{WEBHOOK_BASE_URL}/status",
            status_callback_method="POST")
        call_sessions[call.sid] = {"chat_id": chat_id, "phone": numero, "accion": accion}
        await update.message.reply_text(f"✅ Llamada iniciada\n🆔 `{call.sid}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error:\n`{e}`", parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "ver_planes":
        keyboard = [[InlineKeyboardButton("📞 Contactar Soporte", url="https://t.me/heavyhittersrd")]]
        await query.edit_message_text(
            "💼 *Planes*\n━━━━━━━━━━━━━\n🥉 1 Día — $30\n🥈 3 Días — $70\n🥇 1 Semana — $100\n👑 1 Mes — $300\n\n📞 Llamadas ilimitadas",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "tengo_key":
        context.user_data["esperando_key"] = True
        await query.edit_message_text("🔑 Escribe tu key:\n`HVY-XXXX-XXXX-XXXX`", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    chat_id = update.effective_chat.id
    BOTONES = {"📞 Cobrar": "cobrar", "📅 Confirmar": "confirmar", "🔔 Recordatorio": "recordatorio", "📊 Encuesta": "encuesta"}

    if context.user_data.get("esperando_key"):
        context.user_data.pop("esperando_key")
        result, error = redeem_key(texto.upper().strip(), chat_id, update.effective_user.username or str(chat_id))
        if error:
            await update.message.reply_text(error)
        else:
            expires = datetime.fromisoformat(result["expires_at"])
            await update.message.reply_text(f"🎉 *¡Activado!*\n📋 {result['plan']}\n📅 {expires.strftime('%d/%m/%Y')}", parse_mode="Markdown", reply_markup=main_keyboard())
        return

    if texto in BOTONES:
        if not is_user_active(chat_id):
            await update.message.reply_text("❌ Sin plan activo. Usa /redeem")
            return
        context.user_data["accion_pendiente"] = BOTONES[texto]
        await update.message.reply_text("📱 Escribe el número:\n`+13023451233`", parse_mode="Markdown")
        return

    if texto == "🔑 Redeem Key":
        context.user_data["esperando_key"] = True
        await update.message.reply_text("🔑 Escribe tu key:\n`HVY-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return
    if texto == "ℹ️ Mi Plan":
        await cmd_miplan(update, context)
        return
    if texto == "🛒 Comprar Plan":
        await update.message.reply_text("Selecciona:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Ver Planes", callback_data="ver_planes")]]))
        return
    if texto == "📞 Soporte":
        await update.message.reply_text("📞 *Soporte HeavyHitters*\n@heavyhittersrd", parse_mode="Markdown")
        return

    if "accion_pendiente" in context.user_data:
        if not is_user_active(chat_id):
            await update.message.reply_text("❌ Sin plan activo.")
            return
        accion = context.user_data.pop("accion_pendiente")
        numero = texto if texto.startswith("+") else "+" + texto
        await hacer_llamada(update, chat_id, numero, accion)
        return

    partes = texto.upper().split()
    if len(partes) == 2 and partes[0] in {"COBRAR", "CONFIRMAR", "RECORDATORIO", "ENCUESTA"}:
        if not is_user_active(chat_id):
            await update.message.reply_text("❌ Sin plan activo.")
            return
        await hacer_llamada(update, chat_id, partes[1] if partes[1].startswith("+") else "+" + partes[1], partes[0].lower())
        return

    await update.message.reply_text("Usa el menú o escribe: `COBRAR +13023451233`", parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def setup_webhook(app):
    webhook_url = f"{WEBHOOK_BASE_URL}/telegram/{TELEGRAM_TOKEN}"
    await app.bot.set_webhook(url=webhook_url)
    log.info(f"✅ Webhook configurado: {webhook_url}")

def main():
    global telegram_app
    init_db()

    telegram_app = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("genkey", cmd_genkey))
    telegram_app.add_handler(CommandHandler("redeem", cmd_redeem))
    telegram_app.add_handler(CommandHandler("miplan", cmd_miplan))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.initialize())
    loop.run_until_complete(setup_webhook(telegram_app))
    loop.run_until_complete(telegram_app.start())

    port = int(os.environ.get("PORT", 8080))
    log.info(f"✅ Bot activo con webhook en puerto {port}")
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    main()
