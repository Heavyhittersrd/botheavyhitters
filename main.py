import os
import logging
import threading
import requests as req
from flask import Flask, request, Response
from signalwire.rest import Client as SignalWireClient
from signalwire.voice_response import VoiceResponse, Gather
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

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

IVR_MENSAJES = {
    "cobrar": "Hola, le llamamos de parte de nuestra empresa. Usted tiene una factura pendiente de pago. Si desea pagar ahora marque 1. Para hablar con un agente marque 2. Para escuchar el monto marque 3.",
    "confirmar": "Hola, le llamamos para confirmar su cita. Si confirma marque 1. Para cancelar marque 2. Para reprogramar marque 3.",
    "recordatorio": "Hola, tiene un pago proximo a vencer. Si ya pago marque 1. Si necesita mas tiempo marque 2. Para hablar con un agente marque 3.",
    "encuesta": "Hola, le llamamos para una encuesta. Si desea participar marque 1. Si no desea participar marque 2.",
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

flask_app = Flask(__name__)

def notify_telegram(chat_id: int, texto: str):
    try:
        req.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.error(f"Error: {e}")

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


def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💳 Cobrar"), KeyboardButton("📅 Confirmar")],
        [KeyboardButton("🔔 Recordatorio"), KeyboardButton("📊 Encuesta")],
        [KeyboardButton("🛒 Comprar Plan"), KeyboardButton("📞 Soporte")],
    ], resize_keyboard=True, persistent=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Bot de Llamadas HeavyHitters*\n\nSelecciona una acción del menú:",
        parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_llamar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💳 Cobrar", callback_data="accion_cobrar"),
         InlineKeyboardButton("📅 Confirmar", callback_data="accion_confirmar")],
        [InlineKeyboardButton("🔔 Recordatorio", callback_data="accion_recordatorio"),
         InlineKeyboardButton("📊 Encuesta", callback_data="accion_encuesta")],
    ]
    await update.message.reply_text("¿Qué acción?", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accion = query.data.replace("accion_", "")
    context.user_data["accion_pendiente"] = accion
    await query.edit_message_text(f"✅ *{accion.upper()}*\n\nEscribe el número:\n`+13023451233`", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    chat_id = update.effective_chat.id

    if texto.lower() in ["start", "inicio", "menu", "hola"]:
        await cmd_start(update, context)
        return

    BOTONES = {"💳 Cobrar": "cobrar", "📅 Confirmar": "confirmar", "🔔 Recordatorio": "recordatorio", "📊 Encuesta": "encuesta"}

    if texto in BOTONES:
        context.user_data["accion_pendiente"] = BOTONES[texto]
        await update.message.reply_text(f"✅ *{texto}*\n\n📱 Escribe el número a llamar:\n`+13023451233`", parse_mode="Markdown")
        return

    if texto == "🛒 Comprar Plan":
        await update.message.reply_text(
            "💼 *Planes Disponibles*\n━━━━━━━━━━━━━\n🥉 1 Día — $30\n🥈 3 Días — $70\n🥇 1 Semana — $100\n👑 1 Mes — $300\n\n📞 Llamadas ilimitadas\n\nContacta: @heavyhittersrd",
            parse_mode="Markdown")
        return

    if texto == "📞 Soporte":
        await update.message.reply_text("📞 *Soporte HeavyHitters*\n\nContacta a nuestro equipo:\n@heavyhittersrd", parse_mode="Markdown")
        return

    partes = texto.upper().split()
    accion = None
    numero = None

    if len(partes) == 2 and partes[0] in {"COBRAR", "CONFIRMAR", "RECORDATORIO", "ENCUESTA"}:
        accion = partes[0].lower()
        numero = partes[1] if partes[1].startswith("+") else "+" + partes[1]
    elif "accion_pendiente" in context.user_data:
        accion = context.user_data.pop("accion_pendiente")
        numero = texto if texto.startswith("+") else "+" + texto

    if accion and numero:
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
    else:
        await update.message.reply_text("⚠️ Escribe: `COBRAR +13023451233` o usa /llamar", parse_mode="Markdown")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("llamar", cmd_llamar))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("✅ Bot activo")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
