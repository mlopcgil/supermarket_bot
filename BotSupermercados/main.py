import os
import logging
from datetime import date
from flask import Flask, request
import telebot
from telebot import types
from dotenv import load_dotenv
from database import (
    init_db, ensure_user, get_user_supermarkets, set_user_supermarkets,
    get_supermarkets, get_prices_for_query, save_session, set_postal_code,
    get_postal_code
)
from shopping import parse_list, compare_shopping_list, format_comparison

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN en el entorno/.env")

bot = telebot.TeleBot(TOKEN, threaded=False)

WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if WEBHOOK_URL:
    webhook_url = WEBHOOK_URL.rstrip("/")

    if not webhook_url.endswith("/telegram-webhook"):
        raise ValueError("WEBHOOK_URL debe terminar en /telegram-webhook")

    if bot.set_webhook(url=webhook_url):
        log.info("Webhook configurado correctamente")
    else:
        raise RuntimeError("No se pudo configurar el webhook")

app = Flask(__name__)
init_db()

# Estado conversacional sencillo en memoria
pending = {}
SUPERMARKET_NAMES = ["Mercadona", "Carrefour", "Lidl", "DIA", "Alcampo", "Aldi", "Eroski", "Ahorramás"]

def user_id(message):
    return message.from_user.id

def ensure(message):
    ensure_user(user_id(message), message.from_user.username, message.from_user.first_name)

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("Comparar lista", "Mis Supermercados")
    markup.row("Configurar CP", "Ayuda")
    return markup

def supermarkets_markup(selected=None):
    selected = set(selected or [])
    markup = types.InlineKeyboardMarkup(row_width=2)
    for row_start in range(0, len(SUPERMARKET_NAMES), 2):
        row = []
        for name in SUPERMARKET_NAMES[row_start:row_start + 2]:
            mark = "✅" if name in selected else "⬜"
            row.append(types.InlineKeyboardButton(f"{mark} {name}", callback_data=f"pref:{name}"))
        markup.row(*row)
    markup.row(types.InlineKeyboardButton("Guardar preferencias", callback_data="pref:save"))
    return markup

@bot.message_handler(commands=["start", "help"])
def start(message):
    ensure(message)
    bot.send_message(
        message.chat.id,
        "🛒 *Tu comparador de supermercados*\n\n"
        "Puedes usar los botones de abajo o enviarme directamente tu lista de la compra:\n"
        "`yogur, manzanas, huevos, leche`\n\n"
        "Comandos y accesos rápidos:\n"
        "• *Comparar lista*: enviar tu lista de la compra\n"
        "• *Mis Supermercados*: elegir tus tiendas habituales\n"
        "• *Configurar CP*: guardar tu código postal\n"
        "• *Ayuda*: ver estas instrucciones",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == "Ayuda" or m.text == "/ayuda")
def help_command(message):
    start(message)

@bot.message_handler(func=lambda m: m.text == "Mis Supermercados" or m.text == "/supermercados")
def preferences(message):
    ensure(message)
    selected = get_user_supermarkets(user_id(message))
    pending[user_id(message)] = {"mode": "preferences", "selected": selected}
    bot.send_message(
        message.chat.id,
        "Selecciona tus supermercados habituales. Pulsa para marcar/desmarcar y guarda:",
        reply_markup=supermarkets_markup(selected)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("pref:"))
def preference_callback(call):
    uid = call.from_user.id
    state = pending.setdefault(uid, {"mode": "preferences", "selected": get_user_supermarkets(uid)})
    if call.data == "pref:save":
        chosen = state.get("selected", [])
        set_user_supermarkets(uid, chosen)
        state["mode"] = None
        bot.answer_callback_query(call.id, "Preferencias guardadas")
        bot.edit_message_text(
            "Preferencias guardadas: " + (", ".join(chosen) if chosen else "ningún supermercado seleccionado") +
            "\nPuedes cambiarlas cuando quieras con el botón 🏪 Mis Supermercados.",
            call.message.chat.id, call.message.message_id
        )
        return
    name = call.data.split(":", 1)[1]
    selected = state.setdefault("selected", [])
    if name in selected:
        selected.remove(name)
    else:
        selected.append(name)
    bot.answer_callback_query(call.id)
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=supermarkets_markup(selected))

@bot.message_handler(func=lambda m: m.text == "Configurar CP" or m.text.startswith("/cp"))
def postal_code(message):
    ensure(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip().isdigit() and len(parts[1].strip()) == 5:
        set_postal_code(user_id(message), parts[1].strip())
        bot.reply_to(message, "Código postal guardado correctamente.", reply_markup=get_main_keyboard())
    else:
        msg = bot.send_message(message.chat.id, "Escribe tu código postal (ejemplo: 28001):")
        bot.register_next_step_handler(msg, process_cp)

def process_cp(message):
    ensure(message)
    cp = (message.text or "").strip()
    if cp.isdigit() and len(cp) == 5:
        set_postal_code(user_id(message), cp)
        bot.send_message(message.chat.id, f"Código postal {cp} guardado.", reply_markup=get_main_keyboard())
    else:
        bot.send_message(message.chat.id, "Código postal no válido. Debe tener 5 dígitos (ejemplo: 28001).", reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda m: m.text == "Comparar lista" or m.text == "/comparar")
def compare_command(message):
    ensure(message)
    msg = bot.send_message(message.chat.id, "Escribe los productos de tu lista separados por comas o saltos de línea (ej: yogur, manzanas, huevos):")
    bot.register_next_step_handler(msg, process_list)

def process_list(message):
    ensure(message)
    items = parse_list(message.text or "")
    if not items:
        bot.reply_to(message, "No he detectado productos. Prueba a escribir: `yogur, manzanas, huevos`", parse_mode="Markdown", reply_markup=get_main_keyboard())
        return
    uid = user_id(message)
    postal_code = get_postal_code(uid)
    if not postal_code:
        bot.send_message(
            message.chat.id,
            "Antes de comparar necesito tu código postal. Configúralo con el botón 📍 Configurar CP o con /cp 28001.",
            reply_markup=get_main_keyboard()
        )
        return
    preferred = get_user_supermarkets(uid)
    if not preferred:
        bot.send_message(message.chat.id, "Primero elige tus supermercados habituales pulsando en 🏪 Mis Supermercados.", reply_markup=get_main_keyboard())
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    for name in SUPERMARKET_NAMES:
        if name not in preferred:
            markup.add(types.InlineKeyboardButton(f"+ {name} solo esta compra", callback_data=f"extra:{name}"))
    markup.add(types.InlineKeyboardButton("Comparar precios ahora", callback_data="run:"))
    pending[uid] = {"mode": "shopping", "items": items, "stores": list(preferred)}
    bot.send_message(
        message.chat.id,
        "Lista: " + ", ".join(items) + "\n\nTiendas habituales: " + ", ".join(preferred) +
        "\nPuedes añadir algún supermercado extra solo para esta compra o pulsar en comparar:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("extra:") or call.data == "run:")
def shopping_callback(call):
    uid = call.from_user.id
    state = pending.get(uid)
    if not state or state.get("mode") != "shopping":
        bot.answer_callback_query(call.id, "La sesión ha caducado. Envía la lista de nuevo.")
        return
    if call.data.startswith("extra:"):
        name = call.data.split(":", 1)[1]
        if name not in state["stores"]:
            state["stores"].append(name)
        bot.answer_callback_query(call.id, f"{name} añadido solo a esta compra")
        bot.edit_message_text(
            "Lista: " + ", ".join(state["items"]) + "\nTiendas para esta compra: " + ", ".join(state["stores"]) +
            "\n\nPulsa el botón cuando estés listo.",
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Comparar precios ahora", callback_data="run:")
            )
        )
        return
    bot.answer_callback_query(call.id, "Consultando precios en tiempo real…")
    items, stores = state["items"], state["stores"]
    try:
        prices = get_prices_for_query(items, stores, get_postal_code(uid))
        result = compare_shopping_list(items, stores, prices)
        save_session(uid, items, stores, result)
        text = format_comparison(result)
        bot.send_message(call.message.chat.id, text, disable_web_page_preview=True, parse_mode="Markdown", reply_markup=get_main_keyboard())
    except Exception:
        log.exception("Error comparando lista")
        bot.send_message(call.message.chat.id, "No he podido completar la comparación. Inténtalo de nuevo en unos momentos.", reply_markup=get_main_keyboard())
    finally:
        pending.pop(uid, None)

@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"))
def free_text_list(message):
    process_list(message)

@app.route("/")
def health():
    return "Bot de supermercados funcionando", 200

@app.route("/telegram-webhook", methods=["POST"])
def webhook():
    if request.headers.get("content-type", "").split(";")[0] == "application/json":
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
