import os
import logging
import threading
import requests as req
import sqlite3
import secrets
import string
from datetime import datetime, timedelta
from flask import Flask, request, Response
from signalwire.rest import Client as SignalWireClient
from signalwire.voice_response import VoiceResponse, Gather
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
SW_PROJECT_ID    = os.environ["SW_PROJECT_ID"]
SW_AUTH_TOKEN    = os.environ["SW_AUTH_TOKEN"]
SW_SPACE_URL     = os.environ["SW_SPACE_URL"]
SW_NUMBER        = os.environ["SW_NUMBER"]
WEBHOOK_BASE_URL = os.environ["WEBHOOK_BASE_URL"]
ADMIN_CHAT_ID    = int(os.environ.get("ADMIN_CHAT_ID", "0"))

sw_client = SignalWireClient(SW_PROJECT_ID, SW_AUTH_TOKEN, signalwire_space_url=SW_SPACE_URL)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
call_sessions = {}

# ─── BASE DE DATOS ─────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("/app/bot.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS keys (
        key TEXT PRIMARY KEY, plan TEXT, days INTEGER, created_at TEXT,
        redeemed_by INTEGER DEFAULT NULL, expires_at TEXT DEFAULT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY, plan TEXT, expires_at TEXT)""")
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect("/app/bot.db")
    conn.row_factory = sqlite3.Row
    return conn

def is_active(chat_id: int) -> bool:
    if chat_id == ADMIN_CHAT_ID:
        return True
    conn = get_db()
    row = conn.cursor().execute("SELECT expires_at FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
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

def redeem_key(key: str, chat_id: int):
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
    c.execute("UPDATE keys SET redeemed_by=?, expires_at=? WHERE key=?", (chat_id, expires_at, key))
    c.execute("INSERT INTO users (chat_id, plan, expires_at) VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET plan=?, expires_at=?",
        (chat_id, row["plan"], expires_at, row["plan"], expires_at))
    conn.commit()
    conn.close()
    return {"plan": row["plan"], "days": row["days"], "expires_at": expires_at}, None

IVR_MENSAJES = {
    "cobrar": "Hola, le llamamos de parte de nuestra empresa. Usted tiene una factura pendiente. Si desea pagar ahora marque 1. Para hablar con un agente marque 2. Para escuchar el monto marque 3.",
    "confirmar": "Hola, le llamamos para confirmar su cita. Si confirma marque 1. Para cancelar marque 2. Para reprogramar marque 3.",
    "recordatorio": "Hola, tiene un pago proximo a vencer. Si ya pago marque 1. Si necesita mas tiempo marque 2. Para hablar con un agente marque 3.",
    "encuesta": "Hola, le llamamos para una encuesta. Si desea participar marque 1. Si no desea participar marque 2.",
}
IVR_OPCIONES = {
    "cobrar":      {"1": "💳 Quiere PAGAR", "2": "👤 Quiere agente", "3": "🔊 Escuchar monto"},
    "confirmar":   {"1": "✅ CONFIRMÓ cita", "2": "❌ CANCELÓ cita", "3": "🔄 Reprogramar"},
    "recordatorio":{"1": "✅ Ya pagó", "2": "⏳ Más tiempo", "3": "👤 Quiere agente"},
    "encuesta":    {"1": "✅ Acepta", "2": "❌ Rechaza"},
}
IVR_RESPUESTA = {
    "cobrar":      {"1": "Recibira un enlace de pago pronto.", "2": "Le comunicamos con un agente.", "3": "Su saldo esta en linea."},
    "confirmar":   {"1": "Cita confirmada.", "2": "Cita cancelada.", "3": "Le contactaremos pronto."},
    "recordatorio":{"1": "Pago registrado, gracias.", "2": "Un agente le contactara.", "3": "Le transferimos."},
    "encuesta":    {"1": "Gracias por participar.", "2": "Que tenga buen dia."},
}

flask_app = Flask(__name__)

def notify_telegram(chat_id, texto):
    try:
        req.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.error(e)

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
    opcion = IVR_OPCIONES.get(action, {}).get(digit, f"Marcó: {digit}")
    notify_telegram(chat_id, f"📞 *Respuesta*\n📱 `{to_number}`\n🎯 `{action.upper()}`\n🔢 *{digit}*\n📋 {opcion}")
    response = VoiceResponse()
    response.say(IVR_RESPUESTA.get(action, {}).get(digit, "Gracias."), language="es-MX")
    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/status", methods=["POST"])
def call_status():
    status      = request.form.get("CallStatus", "")
    to_number   = request.form.get("To", "")
    call_sid    = request.form.get("CallSid", "")
    answered_by = request.form.get("AnsweredBy", "")
    session     = call_sessions.get(call_sid, {})
    chat_id     = session.get("chat_id", ADMIN_CHAT_ID)

    log.info(f"Status: {status} | AnsweredBy: {answered_by} | To: {to_number}")

    if status == "initiated":
        notify_telegram(chat_id, f"📞 *Llamada en progreso...*\n📱 `{to_number}`")
    elif status == "ringing":
        notify_telegram(chat_id, f"🔔 *Timbrando...*\n📱 `{to_number}`")
    elif status == "in-progress":
        if answered_by == "human":
            notify_telegram(chat_id, f"👤 *Humano detectado*\n📱 `{to_number}`")
        elif "machine" in answered_by:
            notify_telegram(chat_id, f"🤖 *Buzón de voz detectado*\n📱 `{to_number}`")
        else:
            notify_telegram(chat_id, f"✅ *Llamada contestada*\n📱 `{to_number}`")
    elif status == "no-answer":
        notify_telegram(chat_id, f"📵 *No contestó*\n📱 `{to_number}`")
    elif status == "busy":
        notify_telegram(chat_id, f"📶 *Línea ocupada*\n📱 `{to_number}`")
    elif status == "failed":
        notify_telegram(chat_id, f"❌ *Llamada fallida*\n📱 `{to_number}`")
    elif status == "canceled":
        notify_telegram(chat_id, f"🚫 *Llamada cancelada*\n📱 `{to_number}`")
    elif status == "completed":
        notify_telegram(chat_id, f"📴 *Llamada finalizada*\n📱 `{to_number}`")
    return "", 204

def menu_sin_key():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔑 Redeem Key")],
        [KeyboardButton("💼 Ver Planes")],
        [KeyboardButton("📞 Soporte")],
    ], resize_keyboard=True)

def menu_con_key():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💳 Cobrar"), KeyboardButton("📅 Confirmar")],
        [KeyboardButton("🔔 Recordatorio"), KeyboardButton("📊 Encuesta")],
        [KeyboardButton("🛒 Comprar Plan"), KeyboardButton("📞 Soporte")],
    ], resize_keyboard=True)

def is_active(chat_id: int) -> bool:
    if chat_id == ADMIN_CHAT_ID:
        return True
    conn = get_db()
    row = conn.cursor().execute("SELECT expires_at FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    if not row:
        return False
    return datetime.now() < datetime.fromisoformat(row["expires_at"])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_active(chat_id):
        await update.message.reply_text(
            "👋 *Bienvenido de vuelta!*\nSelecciona una acción:",
            parse_mode="Markdown", reply_markup=menu_con_key())
    else:
        await update.message.reply_text(
            "👋 *Bienvenido al Bot OTP de HeavyHitters!*\n\nActiva tu plan con una key para comenzar:",
            parse_mode="Markdown", reply_markup=menu_sin_key())

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
    if not context.args:
        await update.message.reply_text("⚠️ Uso: `/redeem HVY-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return
    key = context.args[0].upper().strip()
    result, error = redeem_key(key, chat_id)
    if error:
        await update.message.reply_text(error)
        return
    expires = datetime.fromisoformat(result["expires_at"])
    await update.message.reply_text(
        f"🎉 *¡Key activada!*\n📋 Plan: *{result['plan']}*\n📅 Expira: *{expires.strftime('%d/%m/%Y')}*\n\nYa puedes usar el bot.",
        parse_mode="Markdown", reply_markup=menu_con_key())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    chat_id = update.effective_chat.id

    if texto == "🔑 Redeem Key":
        context.user_data["esperando_key"] = True
        await update.message.reply_text("🔑 Escribe tu key:\n`HVY-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return

    if context.user_data.get("esperando_key"):
        context.user_data.pop("esperando_key")
        key = texto.upper().strip()
        result, error = redeem_key(key, chat_id)
        if error:
            await update.message.reply_text(error)
        else:
            expires = datetime.fromisoformat(result["expires_at"])
            await update.message.reply_text(
                f"🎉 *¡Key activada!*\n📋 {result['plan']}\n📅 Expira: {expires.strftime('%d/%m/%Y')}",
                parse_mode="Markdown", reply_markup=menu_con_key())
        return

    if texto == "💼 Ver Planes":
        await update.message.reply_text(
            "💼 *Planes HeavyHitters OTP*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            "🥉 *1 Día* — $35\n"
            "🥈 *3 Días* — $79\n"
            "🥇 *1 Semana* — $129\n"
            "👑 *1 Mes* — $299\n\n"
            "✅ Llamadas ilimitadas incluidas\n"
            "✅ Cobros, confirmaciones, recordatorios y más\n"
            "✅ Activación inmediata con key\n\n"
            "📩 Para comprar contacta: @heavyhittersrd",
            parse_mode="Markdown")
        return

    if texto == "📞 Soporte":
        await update.message.reply_text("📞 Contacta: @heavyhittersrd")
        return

    if not is_active(chat_id):
        await update.message.reply_text("❌ Necesitas activar una key primero.", reply_markup=menu_sin_key())
        return

    BOTONES = {
        "💳 Cobrar": "cobrar",
        "📅 Confirmar": "confirmar",
        "🔔 Recordatorio": "recordatorio",
        "📊 Encuesta": "encuesta"
    }

    if texto in BOTONES:
        context.user_data["accion"] = BOTONES[texto]
        await update.message.reply_text("📱 Escribe el número a llamar:\n`+13023451233`", parse_mode="Markdown")
        return

    if texto == "🛒 Comprar Plan":
        await update.message.reply_text(
            "💼 *Planes*\n🥉 1 Día — $30\n🥈 3 Días — $70\n🥇 1 Semana — $100\n👑 1 Mes — $300\n\nContacta: @heavyhittersrd",
            parse_mode="Markdown")
        return

    if "accion" in context.user_data:
        accion = context.user_data.pop("accion")
        numero = texto if texto.startswith("+") else "+" + texto
        try:
            await update.message.reply_text(f"📞 Llamando a `{numero}`...", parse_mode="Markdown")
            call = sw_client.calls.create(
                to=numero, from_=SW_NUMBER,
                url=f"{WEBHOOK_BASE_URL}/voice/{accion}",
                status_callback=f"{WEBHOOK_BASE_URL}/status",
                status_callback_method="POST")
            call_sessions[call.sid] = {"chat_id": chat_id}
            await update.message.reply_text("✅ Llamada iniciada")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")
        return

    partes = texto.upper().split()
    if len(partes) == 2 and partes[0] in {"COBRAR", "CONFIRMAR", "RECORDATORIO", "ENCUESTA"}:
        accion = partes[0].lower()
        numero = partes[1] if partes[1].startswith("+") else "+" + partes[1]
        try:
            await update.message.reply_text(f"📞 Llamando a `{numero}`...", parse_mode="Markdown")
            call = sw_client.calls.create(
                to=numero, from_=SW_NUMBER,
                url=f"{WEBHOOK_BASE_URL}/voice/{accion}",
                status_callback=f"{WEBHOOK_BASE_URL}/status",
                status_callback_method="POST")
            call_sessions[call.sid] = {"chat_id": chat_id}
            await update.message.reply_text("✅ Llamada iniciada")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")
        return

    await update.message.reply_text("Usa el menú.", reply_markup=menu_con_key() if is_active(chat_id) else menu_sin_key())

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False)

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("✅ Bot activo")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
