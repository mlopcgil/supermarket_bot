import re
import unicodedata
from collections import defaultdict


def normalize(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def parse_list(text):
    parts = re.split(r"[,;\n]+", text or "")
    return list(dict.fromkeys(
        p.strip(" -•\t") for p in parts if p.strip(" -•\t")
    ))


def _date_key(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _package_signature(product):
    """
    Identificador conservador para comparar envases en una cesta.

    No basta con que dos productos tengan el mismo nombre:
    también deben tener el mismo tamaño de envase.
    """
    canonical = (product.get("canonical_key") or "").strip()

    if canonical:
        product_id = ("canonical", canonical)
    else:
        name = normalize(
            product.get("normalized_name") or product.get("name") or ""
        )
        if not name:
            return None
        product_id = ("name", name)

    package_size = normalize(product.get("package_size") or "")
    unit = normalize(product.get("unit") or "")

    if not package_size or not unit:
        return None

    return product_id, package_size, unit


def compare_shopping_list(items, stores, queried):
    """
    Compara precios por unidad homogénea y calcula los totales de envase.

    La cesta solo tiene un ganador si existen al menos dos supermercados
    que ofrecen referencias y tamaños de envase equivalentes para todos
    los productos de la lista.
    """
    output = []

    for entry in queried:
        prices = entry.get("prices", [])
        by_store = defaultdict(list)

        for product in prices:
            if product.get("supermarket") in stores:
                by_store[product["supermarket"]].append(product)

        store_prices = {}

        for store, candidates in by_store.items():
            # Se conserva el candidato con mejor coincidencia y fecha.
            candidates.sort(
                key=lambda p: (
                    p.get("_match_score", 0),
                    _date_key(p.get("captured_at")),
                ),
                reverse=True,
            )
            store_prices[store] = candidates[0]

        # Comparación individual por unidad de referencia.
        candidates = {
            store: product
            for store, product in store_prices.items()
            if product.get("unit_price") is not None
            and product.get("unit")
        }

        groups = defaultdict(dict)

        for store, product in candidates.items():
            canonical = (product.get("canonical_key") or "").strip()

            if canonical:
                signature = ("canonical", canonical)
            else:
                signature = (
                    "name-format",
                    normalize(
                        product.get("normalized_name")
                        or product.get("name")
                        or ""
                    ),
                    normalize(product.get("package_size") or ""),
                )

            groups[signature][store] = product

        comparable = max(groups.values(), key=len) if groups else {}

        units = {
            str(product["unit"]).lower()
            for product in comparable.values()
        }

        can_compare = len(comparable) >= 2 and len(units) == 1

        cheapest = (
            min(
                comparable,
                key=lambda store: comparable[store]["unit_price"],
            )
            if can_compare
            else None
        )

        unit_values = (
            [p["unit_price"] for p in comparable.values()]
            if can_compare
            else []
        )

        savings = (
            round(max(unit_values) - min(unit_values), 2)
            if len(unit_values) >= 2
            else None
        )

        output.append({
            "query": entry["query"],
            "store_prices": store_prices,
            "cheapest": cheapest,
            "savings": savings,
            "comparison_unit": next(iter(units)) if can_compare else None,
        })

    # Totales de envases encontrados por supermercado.
    totals = {}

    for store in stores:
        found = []
        missing = []

        for item in output:
            product = item["store_prices"].get(store)
            pack_price = product.get("pack_price") if product else None

            if pack_price is None and product:
                # Compatibilidad con registros antiguos.
                if product.get("unit") == "envase":
                    pack_price = product.get("unit_price")

            if pack_price is not None:
                found.append((item["query"], pack_price))
            else:
                missing.append(item["query"])

        totals[store] = {
            "total": round(sum(value for _, value in found), 2),
            "found": len(found),
            "missing": missing,
        }

    # Comprobación estricta de equivalencia de la cesta.
    #
    # Para cada producto se agrupan las tiendas que tienen exactamente
    # la misma referencia, tamaño de envase y unidad.
    #
    # Después se calcula la intersección de tiendas que cumplen esa
    # condición en TODOS los productos de la lista.
    comparable_stores = None
    basket_comparison_reasons = []

    if not output:
        basket_comparison_reasons.append("La lista está vacía.")
    else:
        for item in output:
            signatures = defaultdict(set)

            for store, product in item["store_prices"].items():
                signature = _package_signature(product)

                if signature is not None:
                    signatures[signature].add(store)

            valid_groups = [
                stores_with_product
                for stores_with_product in signatures.values()
                if len(stores_with_product) >= 2
            ]

            stores_for_item = (
                max(valid_groups, key=len)
                if valid_groups
                else set()
            )

            if not stores_for_item:
                basket_comparison_reasons.append(
                    f"No hay dos tiendas con la misma referencia y "
                    f"formato para «{item['query']}»."
                )

            if comparable_stores is None:
                comparable_stores = set(stores_for_item)
            else:
                comparable_stores.intersection_update(stores_for_item)

    comparable_stores = comparable_stores or set()

    # Solo se declara ganador si las mismas dos o más tiendas pueden
    # compararse en todos los productos de la cesta.
    eligible = [
        store
        for store in comparable_stores
        if totals.get(store, {}).get("found") == len(output)
    ]

    best_total = (
        min(eligible, key=lambda store: totals[store]["total"])
        if len(eligible) >= 2
        else None
    )

    basket_comparable = best_total is not None

    if len(comparable_stores) < 2 and output:
        basket_comparison_reasons.append(
            "No hay al menos dos supermercados que compartan "
            "referencias y formatos equivalentes en toda la lista."
        )

    return {
        "items": output,
        "totals": totals,
        "best_total": best_total,
        "item_count": len(output),
        "basket_comparable": basket_comparable,
        "basket_comparable_stores": sorted(comparable_stores),
        "basket_comparison_reasons": list(dict.fromkeys(
            basket_comparison_reasons
        )),
    }


def format_comparison(result):
    lines = ["🛒 *Comparativa de tu lista*"]

    for item in result["items"]:
        lines.append(f"\n*{item['query']}*")

        if not item["store_prices"]:
            lines.append(
                "  No hay precios recientes disponibles para este producto."
            )
            continue

        for store, product in sorted(
            item["store_prices"].items(),
            key=lambda kv: (
                kv[1].get("pack_price") is None,
                kv[1].get("pack_price") or float("inf"),
            ),
        ):
            pack_price = product.get("pack_price")

            if pack_price is None and product.get("unit") == "envase":
                pack_price = product.get("unit_price")

            captured_at = product.get("captured_at")

            age = (
                captured_at.strftime("%d/%m/%Y")
                if hasattr(captured_at, "strftime")
                else str(captured_at or "fecha desconocida")
            )

            raw_data = product.get("raw_data") or {}

            if raw_data.get("date_estimated"):
                age += " (estimada)"

            location_label = raw_data.get("location_label")

            if location_label:
                lines.append(
                    f"      Ubicación del precio: {location_label}; "
                    "no es una tarifa exacta de tu CP."
                )
            elif raw_data.get(
                "location_scope"
            ) == "web_catalog_for_requested_postal_code_unverified":
                lines.append(
                    "      Precio observado en la web; la tienda/zona "
                    "de entrega no ha podido verificarse."
                )

            source = product.get("source") or "fuente no identificada"

            if pack_price is not None:
                lines.append(
                    f"  • {store}: envase *{pack_price:.2f} €* "
                    f"— dato {age} — fuente: {source}"
                )
            else:
                lines.append(
                    f"  • {store}: precio de envase no disponible "
                    f"— dato {age} — fuente: {source}"
                )

            if (
                product.get("unit_price") is not None
                and product.get("unit")
                and str(product.get("unit")).lower() != "envase"
            ):
                lines.append(
                    f"      Precio de referencia: "
                    f"{product['unit_price']:.2f} €/{product['unit']}"
                )

        if item["cheapest"]:
            lines.append(
                f"  Más barato por {item['comparison_unit']}: "
                f"*{item['cheapest']}*"
            )
        else:
            lines.append(
                "  No comparo un ganador: faltan precios de referencia "
                "equivalentes."
            )

        if item["savings"] is not None:
            lines.append(
                f"  Diferencia por unidad de referencia: "
                f"{item['savings']:.2f} €/{item['comparison_unit']}"
            )

    lines.append("\n*Coste total por supermercado (precios de envase)*")

    for store, data in sorted(
        result["totals"].items(),
        key=lambda kv: kv[1]["total"],
    ):
        if data["found"] == result["item_count"]:
            lines.append(
                f"  • {store}: *{data['total']:.2f} €* (lista completa)"
            )
        elif data["found"]:
            lines.append(
                f"  • {store}: {data['total']:.2f} € de "
                f"{result['item_count']} productos; faltan: "
                f"{', '.join(data['missing'])}"
            )
        else:
            lines.append(f"  • {store}: sin precios para la lista")

    if result.get("best_total"):
        lines.append(
            f"\n🏆 *Menor coste de la cesta comparable: "
            f"{result['best_total']}*"
        )
    else:
        complete = [
            store
            for store, data in result["totals"].items()
            if data["found"] == result["item_count"]
            and result["item_count"] > 0
        ]

        if complete:
            lines.append(
                "\nℹ️ Hay listas completas, pero no declaro una ganadora: "
                "no se ha podido verificar que las tiendas vendan "
                "exactamente las mismas referencias y tamaños de envase."
            )
        else:
            lines.append(
                "\nℹ️ Los totales mostrados son sumas orientativas de "
                "los envases encontrados. No se comparan como una cesta "
                "equivalente porque faltan productos o formatos coincidentes."
            )

    no_data = {
        store
        for store, data in result["totals"].items()
        if data["found"] == 0
    }

    if "Lidl" in no_data:
        lines.append(
            "\nℹ️ Lidl: no se ha obtenido ninguna oferta verificable "
            "para esta búsqueda. El buscador público puede cambiar o "
            "no devolver precios para todos los productos."
        )

    if no_data.intersection({"Alcampo", "Carrefour"}):
        lines.append(
            "\nℹ️ En Alcampo/Carrefour no se ha podido recuperar un "
            "precio verificable. La web puede bloquear la consulta "
            "o no mostrar los datos del producto."
        )

    lines.append(
        "\nFuente y fecha indicadas por producto. Los precios pueden "
        "variar según zona y disponibilidad."
    )

    return "\n".join(lines)