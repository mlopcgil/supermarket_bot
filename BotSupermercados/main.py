import os
import logging
from datetime import date
from flask import Flask, request
import telebot
from telebot import types
from dotenv import load_dotenv

load_dotenv()

from database import (
    init_db, ensure_user, get_user_supermarkets, set_user_supermarkets,
    get_supermarkets, get_prices_for_query, save_session, set_postal_code,
    get_postal_code
)
from shopping import (
    parse_list,
    compare_shopping_list,
    format_comparison,
    build_product_options,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN en el entorno/.env")

bot = telebot.TeleBot(TOKEN, threaded=False)
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

def send_long_message(chat_id, message, reply_markup=None):
    """Divide mensajes largos para respetar el límite de Telegram."""
    max_length = 3500
    chunks = []
    current_chunk = ""

    for line in message.splitlines(keepends=True):
        if len(current_chunk) + len(line) > max_length:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""

            # Si una sola línea supera el límite, también la dividimos.
            while len(line) > max_length:
                chunks.append(line[:max_length])
                line = line[max_length:]

        current_chunk += line

    if current_chunk:
        chunks.append(current_chunk)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1

        bot.send_message(
            chat_id,
            chunk,
            disable_web_page_preview=True,
            parse_mode="Markdown",
            reply_markup=reply_markup if is_last else None
        )

OPTIONS_PER_PAGE = 6


def option_description(option):
    """Texto descriptivo de una opción y sus precios disponibles."""
    name = option.get("name") or "Producto sin nombre"
    brand = option.get("brand")
    package = option.get("package_size")

    title = name
    if brand and normalize_text(brand) not in normalize_text(name):
        title += f" ({brand})"
    if package:
        title += f" - {package}"

    prices = []
    for product in option["products"]:
        price = product.get("pack_price")
        if price is not None:
            prices.append(
                f"{product['supermarket']}: {price:.2f} €"
            )
        else:
            prices.append(
                f"{product['supermarket']}: precio no disponible"
            )

    return title, ", ".join(prices)


def normalize_text(value):
    import unicodedata
    import re

    value = unicodedata.normalize("NFKD", str(value or "").lower())
    value = "".join(
        c for c in value if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def option_keyboard(options, page):
    markup = types.InlineKeyboardMarkup(row_width=1)

    start = page * OPTIONS_PER_PAGE
    end = start + OPTIONS_PER_PAGE

    for index in range(start, min(end, len(options))):
        title, _ = option_description(options[index])
        label = f"Elegir {index + 1}: {title}"

        if len(label) > 60:
            label = label[:57] + "..."

        markup.add(
            types.InlineKeyboardButton(
                label,
                callback_data=f"choose:{index}"
            )
        )

    navigation = []

    if page > 0:
        navigation.append(
            types.InlineKeyboardButton(
                "⬅️ Anterior",
                callback_data=f"page:{page - 1}"
            )
        )

    if end < len(options):
        navigation.append(
            types.InlineKeyboardButton(
                "Siguiente ➡️",
                callback_data=f"page:{page + 1}"
            )
        )

    if navigation:
        markup.row(*navigation)

    return markup


def show_current_options(chat_id, uid):
    """
    Avanza por los productos:
    - Sin resultados: guarda el producto como no encontrado.
    - Una opción: la selecciona automáticamente.
    - Varias opciones: pregunta al usuario.
    """
    state = pending.get(uid)

    if not state:
        return

    while state["item_index"] < len(state["items"]):
        index = state["item_index"]
        query = state["items"][index]
        options = state["options_by_item"][index]

        if not options:
            state["selected"].append({
                "query": query,
                "prices": []
            })
            state["item_index"] += 1
            continue

        if len(options) == 1:
            state["selected"].append({
                "query": query,
                "prices": options[0]["products"]
            })
            state["item_index"] += 1
            continue

        # Hay varias referencias: el usuario debe elegir.
        state["current_page"] = 0

        title = (
            f"🔎 *Elige la referencia para:* {query}\n\n"
            f"He encontrado {len(options)} opciones.\n"
            "Selecciona la que corresponda a tu producto:\n\n"
        )

        start = 0
        end = min(OPTIONS_PER_PAGE, len(options))

        for option in options[start:end]:
            option_title, prices = option_description(option)
            title += f"• *{option_title}*\n"
            if prices:
                title += f"  {prices}\n"
            title += "\n"

        bot.send_message(
            chat_id,
            title,
            parse_mode="Markdown",
            reply_markup=option_keyboard(options, 0)
        )
        return

    # Ya se han seleccionado todos los productos.
    finish_selected_comparison(chat_id, uid)


def finish_selected_comparison(chat_id, uid):
    state = pending.get(uid)

    if not state:
        return

    items = state["items"]
    stores = state["stores"]
    selected = state["selected"]

    try:
        result = compare_shopping_list(items, stores, selected)
        save_session(uid, items, stores, result)

        text = format_comparison(result)

        fallback_zones = set()

        for entry in selected:
            for price in entry.get("prices", []):
                raw_data = price.get("raw_data") or {}

                if raw_data.get("location_fallback"):
                    fallback_zones.add(
                        str(raw_data.get("zone") or "zona de Madrid")
                    )

        if fallback_zones:
            zones_text = ", ".join(sorted(fallback_zones))
            postal_code = get_postal_code(uid)

            warning = (
                "⚠️ *Aviso sobre la ubicación:* no hay precios "
                "publicados para tu código postal "
                f"{postal_code}. He utilizado precios de otra "
                f"zona disponible ({zones_text}). No son "
                "necesariamente los precios exactos de tu CP.\n\n"
            )

            text = warning + text

        send_long_message(
            chat_id,
            text,
            reply_markup=get_main_keyboard()
        )

    except Exception:
        log.exception("Error comparando productos seleccionados")
        bot.send_message(
            chat_id,
            "No he podido completar la comparación. "
            "Inténtalo de nuevo en unos momentos.",
            reply_markup=get_main_keyboard()
        )

    finally:
        pending.pop(uid, None)

@bot.callback_query_handler(
    func=lambda call: (
        call.data.startswith("choose:")
        or call.data.startswith("page:")
    )
)
def product_selection_callback(call):
    uid = call.from_user.id
    state = pending.get(uid)

    if not state or state.get("mode") != "selecting_products":
        bot.answer_callback_query(
            call.id,
            "La selección ha caducado. Envía la lista de nuevo."
        )
        return

    index = state["item_index"]
    options = state["options_by_item"][index]

    if call.data.startswith("page:"):
        page = int(call.data.split(":")[1])
        max_page = (len(options) - 1) // OPTIONS_PER_PAGE

        if page < 0 or page > max_page:
            bot.answer_callback_query(call.id)
            return

        state["current_page"] = page

        start = page * OPTIONS_PER_PAGE
        end = min(start + OPTIONS_PER_PAGE, len(options))

        query = state["items"][index]
        message_text = (
            f"🔎 *Elige la referencia para:* {query}\n\n"
            f"Opciones {start + 1}-{end} de {len(options)}:\n\n"
        )

        for option in options[start:end]:
            option_title, prices = option_description(option)
            message_text += f"• *{option_title}*\n"
            if prices:
                message_text += f"  {prices}\n"
            message_text += "\n"

        bot.answer_callback_query(call.id)

        bot.edit_message_text(
            message_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=option_keyboard(options, page)
        )
        return

    selected_index = int(call.data.split(":")[1])

    if selected_index < 0 or selected_index >= len(options):
        bot.answer_callback_query(call.id, "Opción no válida.")
        return

    selected_option = options[selected_index]

    state["selected"].append({
        "query": state["items"][index],
        "prices": selected_option["products"]
    })

    state["item_index"] += 1

    bot.answer_callback_query(
        call.id,
        "Referencia seleccionada"
    )

    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )
    except Exception:
        log.warning("No se pudo retirar el teclado de selección")

    show_current_options(call.message.chat.id, uid)

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
    bot.answer_callback_query(
    call.id,
    "Consultando productos disponibles…"
)

    items = state["items"]
    stores = state["stores"]

    try:
        prices = get_prices_for_query(
            items,
            stores,
            get_postal_code(uid)
        )

        options_by_item = []

        for entry in prices:
            options = build_product_options(
                entry.get("prices", [])
            )
            options_by_item.append(options)

        # Se inicia el proceso de selección.
        state["mode"] = "selecting_products"
        state["options_by_item"] = options_by_item
        state["selected"] = []
        state["item_index"] = 0

        show_current_options(call.message.chat.id, uid)

    except Exception:
        log.exception("Error preparando opciones de productos")

        bot.send_message(
            call.message.chat.id,
            "No he podido recuperar las opciones de productos. "
            "Inténtalo de nuevo en unos momentos.",
            reply_markup=get_main_keyboard()
        )

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
