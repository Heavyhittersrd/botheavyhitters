import os
import logging
import threading
import requests as req
from flask import Flask, request, Response
from signalwire.rest import Client as SignalWireClient
from signalwire.voice_response import VoiceResponse, Gather
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
SW_PROJECT_ID    = os.environ["SW_PROJECT_ID"]      # Project ID de SignalWire
SW_AUTH_TOKEN    = os.environ["SW_AUTH_TOKEN"]       # Auth Token de SignalWire
SW_SPACE_URL     = os.environ["SW_SPACE_URL"]        # ej: tuempresa.signalwire.com
SW_NUMBER        = os.environ["SW_NUMBER"]           # ej: +12015551234
WEBHOOK_BASE_URL = os.environ["WEBHOOK_BASE_URL"]    # ej: https://tuapp.railway.app
ADMIN_CHAT_ID    = int(os.environ.get("ADMIN_CHAT_ID", "0"))

# Cliente SignalWire (compatible con Twilio SDK)
sw_client = SignalWireClient(
    SW_PROJECT_ID,
    SW_AUTH_TOKEN,
    signalwire_space_url=SW_SPACE_URL
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── SESIONES EN MEMORIA ──────────────────────────────────────────────────────
# { call_sid: { "chat_id": int, "phone": str, "accion": str } }
call_sessions: dict = {}

# ─── MENSAJES IVR POR ACCIÓN ─────────────────────────────────────────────────
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
        "Hola, le recordamos que tiene un pago próximo a vencer. "
        "Si ya realizó el pago, marque 1. "
        "Si necesita más tiempo, marque 2. "
        "Para hablar con un agente, marque 3."
    ),
    "encuesta": (
        "Hola, le llamamos para una breve encuesta de satisfacción. "
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
    "cobrar": {
        "1": "Perfecto. En breve recibirá un enlace de pago. Gracias.",
        "2": "Le comunicamos con un agente. Por favor espere.",
        "3": "Su saldo pendiente está disponible en línea. Visite nuestro sitio web. Gracias.",
    },
    "confirmar": {
        "1": "Excelente. Su cita está confirmada. Hasta pronto.",
        "2": "Entendido. Su cita ha sido cancelada. Puede reagendar en cualquier momento.",
        "3": "Le contactaremos para reprogramar su cita. Hasta pronto.",
    },
    "recordatorio": {
        "1": "Perfecto. Hemos registrado su pago. Gracias.",
        "2": "Entendido. Un agente le contactará para coordinar su pago.",
        "3": "Le transferimos con un agente ahora mismo.",
    },
    "encuesta": {
        "1": "Gracias por participar. Le haremos algunas preguntas breves.",
        "2": "Entendido. Que tenga un buen día. Adiós.",
    },
}

# ─── FLASK — WEBHOOKS DE SIGNALWIRE ───────────────────────────────────────────
flask_app = Flask(__name__)


def notify_telegram(chat_id: int, texto: str):
    """Envía mensaje a Telegram de forma síncrona (desde Flask)."""
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
    """SignalWire llama aquí cuando el cliente contesta. Reproduce el mensaje IVR."""
    response  = VoiceResponse()
    mensaje   = IVR_MENSAJES.get(action, "Por favor marque 1 para confirmar o 2 para cancelar.")
    gather    = Gather(
        num_digits=1,
        action=f"{WEBHOOK_BASE_URL}/gather/{action}",
        method="POST",
        timeout=10,
        language="es-MX",
    )
    gather.say(mensaje, language="es-MX", voice="Polly.Mia")
    response.append(gather)
    # Si no marca nada
    response.say("No recibimos su respuesta. Le contactaremos nuevamente. Adiós.", language="es-MX", voice="Polly.Mia")
    response.hangup()
    return Response(str(response), mimetype="text/xml")


@flask_app.route("/gather/<action>", methods=["POST"])
def gather_webhook(action):
    """SignalWire llama aquí con el dígito que marcó el cliente."""
    digit     = request.form.get("Digits", "?")
    call_sid  = request.form.get("CallSid", "")
    to_number = request.form.get("To", "")

    session  = call_sessions.get(call_sid, {})
    chat_id  = session.get("chat_id", ADMIN_CHAT_ID)

    opciones      = IVR_OPCIONES.get(action, {})
    texto_opcion  = opciones.get(digit, f"Marcó el dígito: *{digit}*")

    mensaje_tg = (
        f"📞 *Respuesta de llamada*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📱 Número: `{to_number}`\n"
        f"🎯 Acción: `{action.upper()}`\n"
        f"🔢 Dígito marcado: *{digit}*\n"
        f"📋 Resultado: {texto_opcion}"
    )
    notify_telegram(chat_id, mensaje_tg)

    # Respuesta de voz al cliente
    response      = VoiceResponse()
    respuestas    = IVR_RESPUESTA_CLIENTE.get(action, {})
    texto_cliente = respuestas.get(digit, "Gracias por su respuesta. Que tenga un buen día.")
    response.say(texto_cliente, language="es-MX", voice="Polly.Mia")
    response.hangup()
    return Response(str(response), mimetype="text/xml")


@flask_app.route("/status", methods=["POST"])
def call_status():
    """Recibe actualizaciones de estado de la llamada."""
    status    = request.form.get("CallStatus", "")
    call_sid  = request.form.get("CallSid", "")
    to_number = request.form.get("To", "")

    session = call_sessions.get(call_sid, {})
    chat_id = session.get("chat_id", ADMIN_CHAT_ID)

    iconos = {
        "no-answer": "📵 No contestó la llamada",
        "busy":      "📶 Línea ocupada",
        "failed":    "❌ Llamada fallida (número incorrecto o bloqueado)",
        "canceled":  "🚫 Llamada cancelada",
        "completed": "✅ Llamada completada",
    }

    if status in iconos and status != "completed":
        notify_telegram(
            chat_id,
            f"📞 *Estado de llamada*\n📱 `{to_number}`\n{iconos[status]}"
        )
    return "", 204


# ─── TELEGRAM BOT ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "👋 *Bot de Llamadas Automatizadas*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Escribe la acción y el número así:\n\n"
        "`COBRAR +13023451233`\n"
        "`CONFIRMAR +13023451233`\n"
        "`RECORDATORIO +13023451233`\n"
        "`ENCUESTA +13023451233`\n\n"
        "O usa /llamar para el menú de botones.\n\n"
        "Comandos disponibles:\n"
        "/llamar — Menú de acciones\n"
        "/ayuda  — Ver esta ayuda"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_llamar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("💳 Cobrar",            callback_data="accion_cobrar"),
            InlineKeyboardButton("📅 Confirmar cita",    callback_data="accion_confirmar"),
        ],
        [
            InlineKeyboardButton("🔔 Recordatorio pago", callback_data="accion_recordatorio"),
            InlineKeyboardButton("📊 Encuesta",          callback_data="accion_encuesta"),
        ],
    ]
    await update.message.reply_text(
        "¿Qué acción deseas ejecutar?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    accion = query.data.replace("accion_", "")
    context.user_data["accion_pendiente"] = accion
    nombres = {"cobrar": "💳 COBRAR", "confirmar": "📅 CONFIRMAR", "recordatorio": "🔔 RECORDATORIO", "encuesta": "📊 ENCUESTA"}
    await query.edit_message_text(
        f"✅ Acción: *{nombres.get(accion, accion)}*\n\nAhora escribe el número a llamar:\n`+13023451233`",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto   = update.message.text.strip()
    chat_id = update.effective_chat.id
    partes  = texto.upper().split()
    accion  = None
    numero  = None

    ACCIONES_VALIDAS = {"COBRAR", "CONFIRMAR", "RECORDATORIO", "ENCUESTA"}

    if len(partes) == 2 and partes[0] in ACCIONES_VALIDAS:
        accion = partes[0].lower()
        numero = partes[1] if partes[1].startswith("+") else "+" + partes[1]

    elif "accion_pendiente" in context.user_data:
        accion = context.user_data.pop("accion_pendiente")
        raw    = texto.strip()
        numero = raw if raw.startswith("+") else "+" + raw

    if accion and numero:
        await hacer_llamada(update, chat_id, numero, accion)
    else:
        await update.message.reply_text(
            "⚠️ Formato incorrecto.\n\nEscribe así:\n`COBRAR +13023451233`\n\nO usa /llamar para el menú.",
            parse_mode="Markdown",
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
            status_callback_event=["no-answer", "busy", "failed", "completed", "canceled"],
        )
        call_sessions[call.sid] = {"chat_id": chat_id, "phone": numero, "accion": accion}
        await update.message.reply_text(
            f"✅ Llamada iniciada exitosamente\n🆔 `{call.sid}`",
            parse_mode="Markdown",
        )
        log.info(f"Llamada iniciada: {call.sid} → {numero} [{accion}]")
    except Exception as e:
        log.error(f"Error al llamar: {e}")
        await update.message.reply_text(f"❌ Error al iniciar la llamada:\n`{e}`", parse_mode="Markdown")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


def main():
    log.info("Iniciando servidor Flask en hilo secundario...")
    threading.Thread(target=run_flask, daemon=True).start()

    log.info("Iniciando bot de Telegram...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("ayuda",  cmd_ayuda))
    app.add_handler(CommandHandler("llamar", cmd_llamar))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("✅ Bot activo y escuchando")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
