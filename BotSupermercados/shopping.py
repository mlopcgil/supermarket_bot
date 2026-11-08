import re
import unicodedata
from collections import defaultdict


def normalize(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def parse_list(text):
    parts = re.split(r"[,;\n]+", text or "")
    return list(dict.fromkeys(p.strip(" -•\t") for p in parts if p.strip(" -•\t")))


def _date_key(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def compare_shopping_list(items, stores, queried):
    """Compara precios por unidad homogénea y calcula cestas con precio de envase."""
    output = []
    for entry in queried:
        prices = entry.get("prices", [])
        by_store = defaultdict(list)
        for p in prices:
            if p.get("supermarket") in stores:
                by_store[p["supermarket"]].append(p)

        store_prices = {}
        for store, candidates in by_store.items():
            # Se conserva el candidato con mejor coincidencia de nombre y fecha.
            candidates.sort(
                key=lambda p: (p.get("_match_score", 0), _date_key(p.get("captured_at"))),
                reverse=True,
            )
            store_prices[store] = candidates[0]

        # Comparar solo referencias homogéneas: misma canonical_key cuando
        # existe; en su defecto, mismo nombre normalizado y mismo formato.
        candidates = {
            store: p for store, p in store_prices.items()
            if p.get("unit_price") is not None and p.get("unit")
        }
        groups = defaultdict(dict)
        for store, product in candidates.items():
            canonical = (product.get("canonical_key") or "").strip()
            if canonical:
                signature = ("canonical", canonical)
            else:
                signature = (
                    "name-format",
                    normalize(product.get("normalized_name") or product.get("name") or ""),
                    normalize(product.get("package_size") or ""),
                )
            groups[signature][store] = product
        comparable = max(groups.values(), key=len) if groups else {}
        units = {str(p["unit"]).lower() for p in comparable.values()}
        can_compare = len(comparable) >= 2 and len(units) == 1
        cheapest = min(comparable, key=lambda s: comparable[s]["unit_price"]) if can_compare else None
        unit_values = [p["unit_price"] for p in comparable.values()] if can_compare else []
        savings = round(max(unit_values) - min(unit_values), 2) if len(unit_values) >= 2 else None
        output.append({
            "query": entry["query"],
            "store_prices": store_prices,
            "cheapest": cheapest,
            "savings": savings,
            "comparison_unit": next(iter(units)) if can_compare else None,
        })

    totals = {}
    for store in stores:
        found = []
        missing = []
        for item in output:
            p = item["store_prices"].get(store)
            pack_price = p.get("pack_price") if p else None
            if pack_price is None and p:
                # Compatibilidad con registros antiguos cuyo unit_price era precio de envase.
                if p.get("unit") == "envase":
                    pack_price = p.get("unit_price")
            if pack_price is not None:
                found.append((item["query"], pack_price))
            else:
                missing.append(item["query"])
        totals[store] = {
            "total": round(sum(v for _, v in found), 2),
            "found": len(found),
            "missing": missing,
        }

    eligible = [s for s, d in totals.items() if d["found"] == len(output) and len(output) > 0]
    best_total = min(eligible, key=lambda s: totals[s]["total"]) if eligible else None
    return {"items": output, "totals": totals, "best_total": best_total, "item_count": len(output)}


def format_comparison(result):
    lines = ["🛒 *Comparativa de tu lista*"]
    for item in result["items"]:
        lines.append(f"\n*{item['query']}*")
        if not item["store_prices"]:
            lines.append("  No hay precios recientes disponibles para este producto.")
            continue
        for store, p in sorted(
            item["store_prices"].items(),
            key=lambda kv: (kv[1].get("pack_price") is None, kv[1].get("pack_price") or float("inf")),
        ):
            pack_price = p.get("pack_price")
            if pack_price is None and p.get("unit") == "envase":
                pack_price = p.get("unit_price")
            age = p["captured_at"].strftime("%d/%m/%Y") if hasattr(p.get("captured_at"), "strftime") else str(p.get("captured_at", "fecha desconocida"))
            raw_data = p.get("raw_data") or {}
            if raw_data.get("date_estimated"):
                age += " (estimada)"
            location_label = raw_data.get("location_label")
            if location_label:
                lines.append(f"      Ubicación del precio: {location_label}; no es una tarifa exacta de tu CP.")
            elif raw_data.get("location_scope") == "web_catalog_for_requested_postal_code_unverified":
                lines.append("      Precio observado en la web; la tienda/zona de entrega no ha podido verificarse.")
            if pack_price is not None:
                lines.append(f"  • {store}: envase *{pack_price:.2f} €* — dato {age}")
            else:
                lines.append(f"  • {store}: precio de envase no disponible — dato {age}")
            if p.get("unit_price") is not None and p.get("unit"):
                lines.append(f"      Precio de referencia: {p['unit_price']:.2f} €/{p['unit']}")
        if item["cheapest"]:
            lines.append(f"  Más barato por {item['comparison_unit']}: *{item['cheapest']}*")
        else:
            lines.append("  No comparo un ganador: faltan precios de referencia equivalentes.")
        if item["savings"] is not None:
            lines.append(f"  Diferencia por unidad de referencia: {item['savings']:.2f} €/{item['comparison_unit']}")

    lines.append("\n*Coste total por supermercado (precios de envase)*")
    for store, data in sorted(result["totals"].items(), key=lambda kv: kv[1]["total"]):
        if data["found"] == result["item_count"]:
            lines.append(f"  • {store}: *{data['total']:.2f} €* (lista completa)")
        elif data["found"]:
            lines.append(f"  • {store}: {data['total']:.2f} € de {result['item_count']} productos; faltan: {', '.join(data['missing'])}")
        else:
            lines.append(f"  • {store}: sin precios para la lista")
    if result["best_total"]:
        lines.append(f"\n🏆 *Menor coste para toda la lista: {result['best_total']}*")
    else:
        lines.append("\nNo hay un supermercado con precios de envase para todos los productos; no comparo totales incompletos como si fueran equivalentes.")

    no_data = {store for store, data in result["totals"].items() if data["found"] == 0}
    if "Lidl" in no_data:
        lines.append("\nℹ️ Lidl España no ofrece catálogo online completo de alimentación; para comparar Lidl hay que importar precios verificados mediante CSV.")
    if no_data.intersection({"Alcampo", "Carrefour"}):
        lines.append("\nℹ️ En Alcampo/Carrefour no se ha podido recuperar un precio verificable de la búsqueda pública. Puede existir en la web, pero no se muestra si la página no expone el dato estructurado.")
    lines.append("\nFuente y fecha indicadas por producto. Los precios pueden variar según zona y disponibilidad.")
    return "\n".join(lines)
