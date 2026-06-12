import os
import logging
import threading
import requests as req
from flask import Flask, request, Response
from signalwire.rest import Client as SignalWireClient
from signalwire.voice_response import VoiceResponse, Gather
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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
    status = request.form.get("CallStatus", "")
    to_number = request.form.get("To", "")
    call_sid = request.form.get("CallSid", "")
    session = call_sessions.get(call_sid, {})
    chat_id = session.get("chat_id", ADMIN_CHAT_ID)
    iconos = {"no-answer": "📵 No contestó", "busy": "📶 Ocupado", "failed": "❌ Falló", "canceled": "🚫 Cancelado"}
    if status in iconos:
        notify_telegram(chat_id, f"📞 *Estado*\n📱 `{to_number}`\n{iconos[status]}")
    return "", 204

def menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💳 Cobrar"), KeyboardButton("📅 Confirmar")],
        [KeyboardButton("🔔 Recordatorio"), KeyboardButton("📊 Encuesta")],
        [KeyboardButton("🛒 Comprar Plan"), KeyboardButton("📞 Soporte")],
    ], resize_keyboard=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Bot de Llamadas HeavyHitters*\n\nSelecciona una acción:",
        parse_mode="Markdown",
        reply_markup=menu()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    chat_id = update.effective_chat.id

    BOTONES = {
        "💳 Cobrar": "cobrar",
        "📅 Confirmar": "confirmar",
        "🔔 Recordatorio": "recordatorio",
        "📊 Encuesta": "encuesta"
    }

    if texto in BOTONES:
        context.user_data["accion"] = BOTONES[texto]
        await update.message.reply_text(f"📱 Escribe el número a llamar:\n`+13023451233`", parse_mode="Markdown")
        return

    if texto == "🛒 Comprar Plan":
        await update.message.reply_text(
            "💼 *Planes*\n🥉 1 Día — $30\n🥈 3 Días — $70\n🥇 1 Semana — $100\n👑 1 Mes — $300\n\nContacta: @heavyhittersrd",
            parse_mode="Markdown")
        return

    if texto == "📞 Soporte":
        await update.message.reply_text("📞 Contacta: @heavyhittersrd")
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
            await update.message.reply_text(f"✅ Llamada iniciada", parse_mode="Markdown")
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
            await update.message.reply_text(f"✅ Llamada iniciada", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")
        return

    await update.message.reply_text("Usa el menú o escribe: `COBRAR +13023451233`", parse_mode="Markdown", reply_markup=menu())

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("✅ Bot activo")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
