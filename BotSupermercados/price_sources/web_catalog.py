"""Conectores prudentes para catálogos web públicos de alimentación.

Solo acepta productos que la página publique con JSON-LD Product + Offer y
precio EUR. No intenta saltarse autenticación, CAPTCHA, geobloqueos ni medidas
anti-bot. Si la web cambia o no expone datos estructurados, devuelve cero filas.

Las URLs de búsqueda se pueden configurar con variables de entorno porque los
sitios cambian sus rutas con cierta frecuencia.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date
from html import unescape
from typing import Any
from urllib.parse import quote_plus, urljoin

import requests

log = logging.getLogger(__name__)
TIMEOUT = int(os.getenv("PRICE_HTTP_TIMEOUT", "20"))
USER_AGENT = os.getenv(
    "PRICE_USER_AGENT",
    "MiCestita/1.0 (comparador de precios; contacto: configurar PRICE_CONTACT)",
)

# Son plantillas de páginas públicas, no APIs oficiales. Se pueden cambiar
# desde Render si los comercios modifican sus rutas.
SEARCH_TEMPLATES = {
    "Alcampo": os.getenv(
        "ALCAMPO_SEARCH_URL_TEMPLATE",
        "https://www.compraonline.alcampo.es/search?query={query}",
    ),
    "Carrefour": os.getenv(
        "CARREFOUR_SEARCH_URL_TEMPLATE",
        "https://www.carrefour.es/supermercado/search?query={query}",
    ),
}

SESSION = requests.Session()
# Si un comercio responde 403/429, se pausa temporalmente ese conector para
# no repetir una petición bloqueada por cada producto de la lista.
_BLOCKED_UNTIL: dict[str, float] = {}
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/ld+json;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
})


def _walk(value: Any):
    """Recorre estructuras JSON-LD, incluidos @graph y listas."""
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk(child)


def _jsonld_documents(html: str):
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        raw = unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            log.debug("JSON-LD no válido en una página de catálogo.")


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _is_product(obj: dict) -> bool:
    typ = obj.get("@type", [])
    return "Product" in ([typ] if isinstance(typ, str) else typ)


def _extract_products(html: str, base_url: str, supermarket: str, query: str, postal_code: str):
    seen = set()
    for document in _jsonld_documents(html):
        for obj in _walk(document):
            if not _is_product(obj):
                continue
            name = str(obj.get("name") or "").strip()
            if not name:
                continue
            offers = obj.get("offers") or {}
            for offer in _as_list(offers):
                if not isinstance(offer, dict):
                    continue
                currency = str(offer.get("priceCurrency") or "EUR").upper()
                if currency != "EUR":
                    continue
                raw_price = offer.get("price")
                if raw_price is None and isinstance(offer.get("priceSpecification"), dict):
                    raw_price = offer["priceSpecification"].get("price")
                try:
                    price = float(str(raw_price).replace(",", "."))
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                availability = str(offer.get("availability") or "").lower()
                if availability and not any(x in availability for x in ("instock", "limitedavailability", "preorder")):
                    continue
                url = urljoin(base_url, str(offer.get("url") or obj.get("url") or base_url))
                sku = str(obj.get("sku") or obj.get("gtin13") or obj.get("gtin") or url)
                identity = (supermarket.lower(), sku, round(price, 4))
                if identity in seen:
                    continue
                seen.add(identity)
                brand = obj.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                # Solo usar un precio unitario si el propio comercio lo expone.
                unit_price = None
                unit = None
                unit_prop = obj.get("unitPriceSpecification") or offer.get("unitPriceSpecification")
                if isinstance(unit_prop, dict):
                    try:
                        reference_quantity = unit_prop.get("referenceQuantity") or {}
                        quantity_value = reference_quantity.get("value", 1) if isinstance(reference_quantity, dict) else 1
                        if float(quantity_value) != 1:
                            raise ValueError("La referencia no es por una unidad estándar")
                        unit_price = float(str(unit_prop.get("price")).replace(",", "."))
                        unit_raw = str(unit_prop.get("unitCode") or unit_prop.get("unitText") or "").lower()
                        unit = {"kgm": "kg", "ltr": "l", "ltq": "l", "c62": "unidad"}.get(unit_raw, unit_raw) or None
                    except (TypeError, ValueError):
                        unit_price, unit = None, None
                yield {
                    "supermarket": supermarket,
                    "name": name,
                    "normalized_name": re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", name.lower())).strip(),
                    "pack_price": price,
                    "price": price,
                    "unit_price": unit_price,
                    "unit": unit,
                    "product_url": url,
                    "brand": str(brand).strip() if brand else None,
                    "source": f"Catálogo web público {supermarket}",
                    "captured_at": date.today().isoformat(),
                    # La tienda puede aplicar precios distintos según ubicación.
                    # Se asocia el registro al CP que se estaba consultando.
                    "postal_code": postal_code,
                    "source_product_id": sku,
                    "raw_data": {
                        "query": query,
                        "requested_postal_code": postal_code,
                        "location_scope": "web_catalog_for_requested_postal_code_unverified",
                        "price_observed_at": date.today().isoformat(),
                        "date_estimated": False,
                        "jsonld_offer": True,
                    },
                }


def search_public_catalog(query: str, stores: list[str], postal_code: str) -> list[dict]:
    """Busca Alcampo y Carrefour en páginas públicas; Lidl no tiene catálogo
    online de alimentación en España y se deja para importación verificada.
    """
    results = []
    for supermarket in ("Alcampo", "Carrefour"):
        if supermarket not in stores:
            continue
        if time.time() < _BLOCKED_UNTIL.get(supermarket, 0):
            log.info("[%s] Conector pausado temporalmente tras una respuesta 403/429 anterior.", supermarket)
            continue
        template = SEARCH_TEMPLATES[supermarket]
        url = template.format(query=quote_plus(query), postal_code=quote_plus(postal_code))
        try:
            response = SESSION.get(url, timeout=TIMEOUT)
            if response.status_code in (403, 429):
                _BLOCKED_UNTIL[supermarket] = time.time() + 900
                log.warning("[%s] La web respondió HTTP %s; se pausa el conector 15 minutos. No equivale a que no haya productos.", supermarket, response.status_code)
                continue
            response.raise_for_status()
            rows = list(_extract_products(response.text, response.url, supermarket, query, postal_code))
            log.info("[%s] Consulta '%s': %s ofertas JSON-LD", supermarket, query, len(rows))
            results.extend(rows)
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (403, 429):
                _BLOCKED_UNTIL[supermarket] = time.time() + 900
            log.warning("[%s] No se pudo consultar el catálogo público: %s", supermarket, exc)
        # Evita lanzar varias búsquedas seguidas contra los comercios.
        time.sleep(0.35)

    if "Lidl" in stores:
        log.info(
            "[Lidl] No se consulta catálogo alimentario: Lidl España no dispone de "
            "compra online de alimentación. Importa precios verificados de folletos/tienda mediante CSV."
        )
    return results
