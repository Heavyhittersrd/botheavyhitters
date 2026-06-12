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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

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
    c.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY,
            plan TEXT,
            days INTEGER,
            created_at TEXT,
            redeemed_by INTEGER DEFAULT NULL,
            redeemed_at TEXT DEFAULT NULL,
            expires_at TEXT DEFAULT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            plan TEXT,
            expires_at TEXT,
            active INTEGER DEFAULT 0
        )
    """)
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
    expires = datetime.fromisoformat(row["expires_at"])
    return datetime.now() < expires

def generate_key(plan: str, days: int) -> str:
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(12))
    key = f"HVY-{random_part[:4]}-{random_part[4:8]}-{random_part[8:12]}"
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO keys (key, plan, days, created_at) VALUES (?, ?, ?, ?)",
        (key, plan, days, datetime.now().isoformat())
    )
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
        return None, "❌ Key inválida. Verifica que la escribiste correctamente."
    if row["redeemed_by"]:
        conn.close()
        return None, "❌ Esta key ya fue usada anteriormente."
    
    days = row["days"]
    plan = row["plan"]
    expires_at = (datetime.now() + timedelta(days=days)).isoformat()
    
    c.execute(
        "UPDATE keys SET redeemed_by=?, redeemed_at=?, expires_at=? WHERE key=?",
        (chat_id, datetime.now().isoformat(), expires_at, key)
    )
    c.execute("""
        INSERT INTO users (chat_id, username, plan, expires_at, active) 
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET plan=?, expires_at=?, active=1
    """, (chat_id, username, plan, expires_at, plan, expires_at))
    conn.commit()
    conn.close()
    return {"plan": plan, "days": days, "expires_at": expires_at}, None

# ─── IVR ──────────────────────────────────────────────────────────────────────
IVR_MENSAJES = {
    "cobrar": (
        "Hola, le llamamos de parte de nuestra empresa. "
        "Usted tiene una factura pendiente de pago. "
        "Si desea pagar ahora, marque 1. "
        "Para hablar con un agente, marque 2. "
        "Para escuchar el monto, marque 3."
    ),
    "confirmar": (
        "Hola, le llamamos para confirmar su cita programada. "
        "Si confirma su asistencia, marque 1. "
        "Si desea cancelar, marque 2. "
        "Para reprogramar, marque 3."
    ),
    "recordatorio": (
        "Hola, le recordamos que tiene un pago proximo a vencer. "
        "Si ya realizo el pago, marque 1. "
        "Si necesita mas tiempo, marque 2. "
        "Para hablar con un agente, marque 3."
    ),
    "encuesta": (
        "Hola, le llamamos para una breve encuesta de satisfaccion. "
        "Si desea participar, marque 1. "
        "Si no desea participar, marque 2."
    ),
}

IVR_OPCIONES = {
    "cobrar":      {"1": "💳 Quiere PAGAR ahora", "2": "👤 Quiere hablar con agente", "3": "🔊 Quiere escuchar el monto"},
    "confirmar":   {"1": "✅ CONFIRMÓ la cita",   "2": "❌ CANCELÓ la cita",          "3": "🔄 Quiere reprogramar"},
    "recordatorio":{"1": "✅ Ya realizó el pago", "2": "⏳ Necesita más tiempo",       "3": "👤 Quiere hablar con agente"},
    "encuesta":    {"1": "✅ Acepta encuesta",     "2": "❌ Rechaza encuesta"},
}

IVR_RESPUESTA_CLIENTE = {
    "cobrar":      {"1": "Perfecto. En breve recibira un enlace de pago. Gracias.", "2": "Le comunicamos con un agente. Por favor espere.", "3": "Su saldo esta disponible en linea. Gracias."},
    "confirmar":   {"1": "Excelente. Su cita esta confirmada. Hasta pronto.", "2": "Entendido. Su cita ha sido cancelada.", "3": "Le contactaremos para reprogramar."},
    "recordatorio":{"1": "Perfecto. Hemos registrado su pago. Gracias.", "2": "Entendido. Un agente le contactara pronto.", "3": "Le transferimos con un agente."},
    "encuesta":    {"1": "Gracias por participar. Que tenga un buen dia.", "2": "Entendido. Que tenga un buen dia."},
}

# ─── FLASK ────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

def notify_telegram(chat_id: int, texto: str):
    try:
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Error notificando Telegram: {e}")

@flask_app.route("/voice/<action>", methods=["POST"])
def voice_webhook(action):
    response = VoiceResponse()
    mensaje  = IVR_MENSAJES.get(action, "Por favor marque 1 para confirmar o 2 para cancelar.")
    gather   = Gather(num_digits=1, action=f"{WEBHOOK_BASE_URL}/gather/{action}", method="POST", timeout=10)
    gather.say(mensaje, language="es-MX")
    response.append(gather)
    response.say("No recibimos su respuesta. Le contactaremos nuevamente.", language="es-MX")
    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/gather/<action>", methods=["POST"])
def gather_webhook(action):
    digit     = request.form.get("Digits", "?")
    call_sid  = request.form.get("CallSid", "")
    to_number = request.form.get("To", "")
    session   = call_sessions.get(call_sid, {})
    chat_id   = session.get("chat_id", ADMIN_CHAT_ID)
    texto_opcion = IVR_OPCIONES.get(action, {}).get(digit, f"Marcó el dígito: *{digit}*")

    notify_telegram(chat_id, (
        f"📞 *Respuesta de llamada*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📱 Número: `{to_number}`\n"
        f"🎯 Acción: `{action.upper()}`\n"
        f"🔢 Dígito marcado: *{digit}*\n"
        f"📋 Resultado: {texto_opcion}"
    ))

    response = VoiceResponse()
    texto_cliente = IVR_RESPUESTA_CLIENTE.get(action, {}).get(digit, "Gracias por su respuesta. Que tenga un buen dia.")
    response.say(texto_cliente, language="es-MX")
    response.hangup()
    return Response(str(response), mimetype="text/xml")

@flask_app.route("/status", methods=["POST"])
def call_status():
    status    = request.form.get("CallStatus", "")
    call_sid  = request.form.get("CallSid", "")
    to_number = request.form.get("To", "")
    session   = call_sessions.get(call_sid, {})
    chat_id   = session.get("chat_id", ADMIN_CHAT_ID)
    iconos    = {"no-answer": "📵 No contestó", "busy": "📶 Línea ocupada", "failed": "❌ Llamada fallida", "canceled": "🚫 Cancelada"}
    if status in iconos:
        notify_telegram(chat_id, f"📞 *Estado*\n📱 `{to_number}`\n{iconos[status]}")
    return "", 204

# ─── TECLADO PRINCIPAL ────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📞 Cobrar"), KeyboardButton("📅 Confirmar")],
            [KeyboardButton("🔔 Recordatorio"), KeyboardButton("📊 Encuesta")],
            [KeyboardButton("🔑 Redeem Key"), KeyboardButton("ℹ️ Mi Plan")],
            [KeyboardButton("🛒 Comprar Plan"), KeyboardButton("📞 Soporte")],
        ],
        resize_keyboard=True,
        persistent=True
    )

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_user_active(chat_id):
        await update.message.reply_text(
            "👋 *Bienvenido de vuelta!*\n\nSelecciona una acción del menú:",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
    else:
        keyboard = [[InlineKeyboardButton("🛒 Ver Planes", callback_data="ver_planes"),
                     InlineKeyboardButton("🔑 Tengo una Key", callback_data="tengo_key")]]
        await update.message.reply_text(
            "👋 *Bienvenido al Bot OTP de HeavyHitters!*\n\n"
            "📞 Llama a tus clientes automáticamente y gestiona cobros, confirmaciones y más.\n\n"
            "Para comenzar necesitas activar un plan:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ No tienes permiso para este comando.")
        return
    
    planes = {"1dia": (1, "1 Día"), "3dias": (3, "3 Días"), "semana": (7, "1 Semana"), "mes": (30, "1 Mes")}
    
    if not context.args or context.args[0].lower() not in planes:
        await update.message.reply_text(
            "⚠️ Uso correcto:\n`/genkey 1dia`\n`/genkey 3dias`\n`/genkey semana`\n`/genkey mes`",
            parse_mode="Markdown"
        )
        return
    
    plan_key = context.args[0].lower()
    days, plan_name = planes[plan_key]
    key = generate_key(plan_name, days)
    
    precios = {"1dia": "$30", "3dias": "$70", "semana": "$100", "mes": "$300"}
    
    await update.message.reply_text(
        f"✅ *Key Generada*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔑 `{key}`\n\n"
        f"📋 Plan: *{plan_name}*\n"
        f"⏱ Duración: *{days} día(s)*\n"
        f"💰 Precio: *{precios[plan_key]}*\n\n"
        f"Envíale esta key al cliente para que la active con /redeem",
        parse_mode="Markdown"
    )

async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or str(chat_id)
    
    if not context.args:
        await update.message.reply_text("⚠️ Uso: `/redeem HVY-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return
    
    key = context.args[0].upper().strip()
    result, error = redeem_key(key, chat_id, username)
    
    if error:
        await update.message.reply_text(error)
        return
    
    expires = datetime.fromisoformat(result["expires_at"])
    await update.message.reply_text(
        f"🎉 *¡Key activada exitosamente!*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📋 Plan: *{result['plan']}*\n"
        f"⏱ Duración: *{result['days']} día(s)*\n"
        f"📅 Expira: *{expires.strftime('%d/%m/%Y %I:%M %p')}*\n\n"
        f"Ya puedes usar todos los comandos del bot!",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def cmd_miplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_CHAT_ID:
        await update.message.reply_text("👑 Eres el administrador — acceso ilimitado.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ No tienes ningún plan activo.\n\nUsa /redeem para activar una key.")
        return
    expires = datetime.fromisoformat(row["expires_at"])
    ahora   = datetime.now()
    if ahora > expires:
        await update.message.reply_text("⚠️ Tu plan ha *expirado*.\n\nContacta soporte para renovar.", parse_mode="Markdown")
        return
    restante = expires - ahora
    dias     = restante.days
    horas    = restante.seconds // 3600
    await update.message.reply_text(
        f"📋 *Tu Plan Activo*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 Plan: *{row['plan']}*\n"
        f"📅 Expira: *{expires.strftime('%d/%m/%Y %I:%M %p')}*\n"
        f"⏳ Tiempo restante: *{dias} días y {horas} horas*\n"
        f"✅ Estado: *Activo*",
        parse_mode="Markdown"
    )

async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ver todas las keys"""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM keys ORDER BY created_at DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No hay keys generadas.")
        return
    texto = "🔑 *Keys generadas (últimas 20)*\n━━━━━━━━━━━━━━━\n\n"
    for row in rows:
        estado = "✅ Usada" if row["redeemed_by"] else "⏳ Disponible"
        texto += f"`{row['key']}` — {row['plan']} — {estado}\n"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    data   = query.data

    if data == "ver_planes":
        texto = (
            "💼 *Planes Disponibles*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            "🥉 *1 Día* — $30\n"
            "🥈 *3 Días* — $70\n"
            "🥇 *1 Semana* — $100\n"
            "👑 *1 Mes* — $300\n\n"
            "📞 Llamadas ilimitadas incluidas\n\n"
            "Para comprar contacta a soporte:"
        )
        keyboard = [[InlineKeyboardButton("📞 Contactar Soporte", url="https://t.me/heavyhittersrd")]]
        await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "tengo_key":
        context.user_data["esperando_key"] = True
        await query.edit_message_text(
            "🔑 Escribe tu key para activarla:\n\nEjemplo: `HVY-XXXX-XXXX-XXXX`",
            parse_mode="Markdown"
        )

async def hacer_llamada(update: Update, chat_id: int, numero: str, accion: str):
    try:
        await update.message.reply_text(
            f"📞 Llamando a `{numero}`\n🎯 Acción: *{accion.upper()}*\n⏳ Espera...",
            parse_mode="Markdown",
        )
        call = sw_client.calls.create(
            to=numero,
            from_=SW_NUMBER,
            url=f"{WEBHOOK_BASE_URL}/voice/{accion}",
            status_callback=f"{WEBHOOK_BASE_URL}/status",
            status_callback_method="POST",
        )
        call_sessions[call.sid] = {"chat_id": chat_id, "phone": numero, "accion": accion}
        await update.message.reply_text(f"✅ Llamada iniciada\n🆔 `{call.sid}`", parse_mode="Markdown")
    except Exception as e:
        log.error(f"Error al llamar: {e}")
        await update.message.reply_text(f"❌ Error:\n`{e}`", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto   = update.message.text.strip()
    chat_id = update.effective_chat.id

    # Mapa de botones del teclado a acciones
    BOTONES = {
        "📞 Cobrar": "cobrar", "📅 Confirmar": "confirmar",
        "🔔 Recordatorio": "recordatorio", "📊 Encuesta": "encuesta"
    }

    # Si está esperando key
    if context.user_data.get("esperando_key"):
        context.user_data.pop("esperando_key")
        username = update.effective_user.username or str(chat_id)
        result, error = redeem_key(texto.upper().strip(), chat_id, username)
        if error:
            await update.message.reply_text(error)
        else:
            expires = datetime.fromisoformat(result["expires_at"])
            await update.message.reply_text(
                f"🎉 *¡Key activada!*\n📋 Plan: *{result['plan']}*\n📅 Expira: *{expires.strftime('%d/%m/%Y')}*",
                parse_mode="Markdown", reply_markup=main_keyboard()
            )
        return

    # Botones del menú
    if texto in BOTONES:
        if not is_user_active(chat_id):
            await update.message.reply_text("❌ No tienes un plan activo. Usa /redeem para activar tu key.")
            return
        context.user_data["accion_pendiente"] = BOTONES[texto]
        await update.message.reply_text(f"📱 Escribe el número a llamar:\n`+13023451233`", parse_mode="Markdown")
        return

    if texto == "🔑 Redeem Key":
        context.user_data["esperando_key"] = True
        await update.message.reply_text("🔑 Escribe tu key:\n`HVY-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return

    if texto == "ℹ️ Mi Plan":
        await cmd_miplan(update, context)
        return

    if texto == "🛒 Comprar Plan":
        keyboard = [[InlineKeyboardButton("Ver Planes", callback_data="ver_planes")]]
        await update.message.reply_text("Selecciona:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if texto == "📞 Soporte":
        await update.message.reply_text("📞 *Soporte HeavyHitters*\n\nContacta a nuestro equipo:\n@heavyhittersrd", parse_mode="Markdown")
        return

    # Si tiene acción pendiente, el texto es el número
    if "accion_pendiente" in context.user_data:
        if not is_user_active(chat_id):
            await update.message.reply_text("❌ No tienes un plan activo.")
            return
        accion = context.user_data.pop("accion_pendiente")
        numero = texto if texto.startswith("+") else "+" + texto
        await hacer_llamada(update, chat_id, numero, accion)
        return

    # Formato texto: ACCION NUMERO
    partes = texto.upper().split()
    if len(partes) == 2 and partes[0] in {"COBRAR", "CONFIRMAR", "RECORDATORIO", "ENCUESTA"}:
        if not is_user_active(chat_id):
            await update.message.reply_text("❌ No tienes un plan activo. Usa /redeem para activar tu key.")
            return
        accion = partes[0].lower()
        numero = partes[1] if partes[1].startswith("+") else "+" + partes[1]
        await hacer_llamada(update, chat_id, numero, accion)
        return

    await update.message.reply_text("⚠️ Usa el menú o escribe: `COBRAR +13023451233`", parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("genkey",  cmd_genkey))
    app.add_handler(CommandHandler("redeem",  cmd_redeem))
    app.add_handler(CommandHandler("miplan",  cmd_miplan))
    app.add_handler(CommandHandler("keys",    cmd_keys))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("✅ Bot activo")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
