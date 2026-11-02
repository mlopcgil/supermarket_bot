import requests
import re
import unicodedata
from datetime import date
import logging

log = logging.getLogger(__name__)

def norm(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()

def search_mercadona(query, zip_code="28001"):
    url = "https://api.mercadona.com/api/v1_1/search/"
    params = {"q": query, "limit": 10}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    results = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("results", [])
            today = date.today().isoformat()
            for p in products:
                price_instructions = p.get("price_instructions", {}) or {}
                unit_price = price_instructions.get("unit_price") or price_instructions.get("bulk_price")
                if unit_price is None:
                    continue
                name = p.get("display_name") or p.get("title") or ""
                results.append({
                    "supermarket": "Mercadona",
                    "name": name,
                    "normalized_name": norm(name),
                    "price": float(unit_price),
                    "unit_price": float(unit_price),
                    "unit": price_instructions.get("unit_name") or "unidad",
                    "product_url": p.get("share_url") or f"https://tienda.mercadona.es/product/{p.get('id')}",
                    "brand": "Hacendado" if "hacendado" in norm(name) else None,
                    "source": "Mercadona API",
                    "captured_at": today,
                    "postal_code": zip_code
                })
    except Exception as e:
        log.warning(f"Error consultando Mercadona API: {e}")
    return results

def search_dia(query, zip_code="28001"):
    url = f"https://www.dia.es/api/v1/search-back/search/products"
    params = {"q": query, "page": 1}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    results = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code != 200:
            url_alt = "https://www.dia.es/api/v1/search"
            resp = requests.get(url_alt, params=params, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("search_result", []) or data.get("products", []) or data.get("items", [])
            today = date.today().isoformat()
            for p in items:
                name = p.get("name") or p.get("display_name") or p.get("title") or ""
                prices = p.get("prices", {}) or {}
                price = prices.get("price") or p.get("price") or prices.get("strikethrough_price")
                if price is None:
                    continue
                results.append({
                    "supermarket": "DIA",
                    "name": name,
                    "normalized_name": norm(name),
                    "price": float(price),
                    "unit_price": float(price),
                    "unit": "unidad",
                    "product_url": f"https://www.dia.es{p.get('url', '')}" if p.get('url') else "https://www.dia.es",
                    "brand": p.get("brand"),
                    "source": "DIA API",
                    "captured_at": today,
                    "postal_code": zip_code
                })
    except Exception as e:
        log.warning(f"Error consultando DIA API: {e}")
    return results

def search_carrefour(query, zip_code="28001"):
    url = "https://www.carrefour.es/cloud-api/v1/search"
    params = {"query": query, "offset": 0, "limit": 10}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }
    results = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("docs", []) or data.get("content", {}).get("docs", [])
            today = date.today().isoformat()
            for p in items:
                name = p.get("display_name") or p.get("title") or ""
                price = p.get("app_price") or p.get("price")
                if price is None:
                    continue
                results.append({
                    "supermarket": "Carrefour",
                    "name": name,
                    "normalized_name": norm(name),
                    "price": float(price),
                    "unit_price": float(price),
                    "unit": "unidad",
                    "product_url": p.get("url") or "https://www.carrefour.es",
                    "brand": p.get("brand"),
                    "source": "Carrefour API",
                    "captured_at": today,
                    "postal_code": zip_code
                })
    except Exception as e:
        log.warning(f"Error consultando Carrefour API: {e}")
    return results

def search_alcampo(query, zip_code="28001"):
    url = "https://www.compraonline.alcampo.es/api/v1/products/search"
    params = {"term": query, "limit": 10}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }
    results = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("products", []) or data.get("entities", [])
            today = date.today().isoformat()
            for p in products:
                name = p.get("name") or p.get("title") or ""
                price = p.get("price", {}).get("amount") if isinstance(p.get("price"), dict) else p.get("price")
                if price is None:
                    continue
                results.append({
                    "supermarket": "Alcampo",
                    "name": name,
                    "normalized_name": norm(name),
                    "price": float(price),
                    "unit_price": float(price),
                    "unit": "unidad",
                    "product_url": f"https://www.compraonline.alcampo.es{p.get('url', '')}" if p.get('url') else "https://www.compraonline.alcampo.es",
                    "brand": p.get("brand"),
                    "source": "Alcampo API",
                    "captured_at": today,
                    "postal_code": zip_code
                })
    except Exception as e:
        log.warning(f"Error consultando Alcampo API: {e}")
    return results

def fetch_live_prices(query, stores, zip_code="28001"):
    all_results = []
    for store in stores:
        store_lower = store.lower()
        if "mercadona" in store_lower:
            all_results.extend(search_mercadona(query, zip_code))
        elif "dia" in store_lower:
            all_results.extend(search_dia(query, zip_code))
        elif "carrefour" in store_lower:
            all_results.extend(search_carrefour(query, zip_code))
        elif "alcampo" in store_lower:
            all_results.extend(search_alcampo(query, zip_code))
    return all_results
