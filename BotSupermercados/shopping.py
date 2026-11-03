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

def compare_shopping_list(items, stores, queried):
    output = []
    for entry in queried:
        prices = entry.get("prices", [])
        # Only compare exact normalized product names, unless only one product row matches.
        by_store = defaultdict(list)
        for p in prices:
            if p.get("unit_price") is not None and p.get("supermarket") in stores:
                by_store[p["supermarket"]].append(p)
        store_prices = {}
        for store, candidates in by_store.items():
            # Avoid claiming equivalence across distinct product names.
            candidates.sort(key=lambda p: (p["captured_at"], p["unit_price"]), reverse=True)
            store_prices[store] = candidates[0]
        values = [p["unit_price"] for p in store_prices.values()]
        cheapest = min(store_prices, key=lambda s: store_prices[s]["unit_price"]) if store_prices else None
        savings = (max(values) - min(values)) if len(values) >= 2 else None
        output.append({
            "query": entry["query"],
            "store_prices": store_prices,
            "cheapest": cheapest,
            "savings": round(savings, 2) if savings is not None else None,
        })
    # basket total by store: sum only products with a matched price; expose coverage count.
    totals = {}
    for store in stores:
        found = [(x["query"], x["store_prices"][store]["unit_price"]) for x in output if store in x["store_prices"]]
        totals[store] = {"total": round(sum(v for _, v in found), 2), "found": len(found), "missing": [x["query"] for x in output if store not in x["store_prices"]]}
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
        for store, p in sorted(item["store_prices"].items(), key=lambda kv: kv[1]["unit_price"]):
            unit = f" ({p['unit']})" if p.get("unit") else ""
            age = p["captured_at"].strftime("%d/%m/%Y") if hasattr(p["captured_at"], "strftime") else str(p["captured_at"])
            lines.append(f"  • {store}: {p['unit_price']:.2f} €{unit} — dato {age}")
            raw = p.get("raw_data") or {}
            ref_price = raw.get("reference_price") if isinstance(raw, dict) else None
            ref_format = raw.get("reference_format") if isinstance(raw, dict) else None
            if ref_price is not None:
                try:
                    lines.append(f"      Referencia: {float(ref_price):.2f} €/" + str(ref_format or "unidad"))
                except (TypeError, ValueError):
                    pass
        if item["cheapest"]:
            lines.append(f"  Más barato: *{item['cheapest']}*")
        if item["savings"] is not None:
            lines.append(f"  Diferencia entre más caro y más barato: {item['savings']:.2f} €")
    lines.append("\n*Coste total por supermercado*")
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
        lines.append("\nNo hay un supermercado con precios disponibles para todos los productos; no comparo totales incompletos como si fueran equivalentes.")
    lines.append("\nFuente de datos y fecha indicadas por producto. La disponibilidad y el precio final pueden variar por zona.")
    return "\n".join(lines)
