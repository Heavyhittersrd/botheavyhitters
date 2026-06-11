# 📞 Bot de Llamadas Automatizadas — Telegram + SignalWire

Bot que te permite desde Telegram ordenar llamadas automáticas a tus clientes con menú IVR (opciones por teclado telefónico).

---

## 🚀 Cómo funciona

```
Tú en Telegram          Cliente (EE.UU.)         Tú en Telegram
──────────────          ─────────────────         ──────────────
COBRAR +1302...   →   📞 Suena el teléfono   →   📲 Marcó opción 1
                       🔊 Escucha el mensaje        💳 Quiere pagar ahora
                       1️⃣ Marca una opción
```

---

## ⚙️ Configuración paso a paso

### Paso 1 — Crear el bot de Telegram
1. Abre Telegram y busca **@BotFather**
2. Escribe `/newbot` y sigue las instrucciones
3. Copia el **token** (ej: `123456789:ABCdef...`)
4. Para saber tu Chat ID, escríbele a **@userinfobot**

### Paso 2 — Crear cuenta SignalWire
1. Ve a [signalwire.com](https://signalwire.com) y crea una cuenta
2. Crea un **Space** (ej: `miempresa`)
3. En el panel ve a **API → Credentials** y copia:
   - Project ID
   - Auth Token
   - Space URL (ej: `miempresa.signalwire.com`)
4. Ve a **Phone Numbers → Buy a Number** y compra uno de EE.UU. (~$1/mes)

### Paso 3 — Deploy en Railway
1. Crea cuenta en [railway.app](https://railway.app) con GitHub
2. Sube esta carpeta a un repositorio de GitHub
3. En Railway: **New Project → Deploy from GitHub repo**
4. Ve a **Variables** y agrega estas 6 variables:

```
TELEGRAM_TOKEN     → token de @BotFather
SW_PROJECT_ID      → Project ID de SignalWire
SW_AUTH_TOKEN      → Auth Token de SignalWire
SW_SPACE_URL       → miempresa.signalwire.com
SW_NUMBER          → +12015551234
WEBHOOK_BASE_URL   → https://tuapp.railway.app  ← Railway te da esta URL
ADMIN_CHAT_ID      → tu chat ID de Telegram
```

5. Railway hace el deploy automáticamente ✅

---

## 📱 Cómo usar el bot

### Opción A — Comando directo
```
COBRAR +13023451233
CONFIRMAR +13023451233
RECORDATORIO +13023451233
ENCUESTA +13023451233
```

### Opción B — Menú visual
Escribe `/llamar` → selecciona la acción con botones → escribe el número

---

## 🎯 Acciones disponibles

| Acción | Mensaje al cliente | Opciones |
|---|---|---|
| COBRAR | Tiene factura pendiente | 1=Pagar, 2=Agente, 3=Ver monto |
| CONFIRMAR | Confirmar cita | 1=Confirmar, 2=Cancelar, 3=Reprogramar |
| RECORDATORIO | Pago próximo a vencer | 1=Ya pagué, 2=Más tiempo, 3=Agente |
| ENCUESTA | Encuesta de satisfacción | 1=Participar, 2=No participar |

---

## ➕ Agregar nuevas acciones

En `main.py` agrega tu acción en 3 diccionarios:

```python
# 1. Mensaje que escucha el cliente
IVR_MENSAJES["nueva"] = "Hola, marque 1 para... o 2 para..."

# 2. Texto que te llega en Telegram
IVR_OPCIONES["nueva"] = {"1": "✅ Opción 1", "2": "❌ Opción 2"}

# 3. Respuesta de voz al cliente según lo que marcó
IVR_RESPUESTA_CLIENTE["nueva"] = {"1": "Gracias...", "2": "Entendido..."}
```

Y agrega el botón en `cmd_llamar()`.

---

## 💰 Costo estimado (500 clientes/mes, ~2 min/llamada)

| Concepto | Costo |
|---|---|
| 1,000 min × $0.008/min | ~$8.00/mes |
| Número SignalWire | ~$1.00/mes |
| Railway (hosting) | Gratis |
| **Total** | **~$9/mes** |
