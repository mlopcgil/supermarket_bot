"""Conectores prudentes para catálogos web públicos.

Solo guarda productos cuando puede extraer un nombre y un precio explícito.
No intenta saltarse CAPTCHA, autenticación, geobloqueos ni medidas anti-bot.

Los conectores de Eroski y Lidl son experimentales: sus webs pueden cambiar
y dejar de exponer los datos esperados.
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
    "MiCestita/1.0 (comparador de precios; configurar PRICE_CONTACT)",
)

SEARCH_TEMPLATES = {
    "Alcampo": os.getenv(
        "ALCAMPO_SEARCH_URL_TEMPLATE",
        "https://www.compraonline.alcampo.es/search?query={query}",
    ),
    "Carrefour": os.getenv(
        "CARREFOUR_SEARCH_URL_TEMPLATE",
        "https://www.carrefour.es/supermercado/search?query={query}",
    ),
    "Eroski": os.getenv(
        "EROSKI_SEARCH_URL_TEMPLATE",
        "https://supermercado.eroski.es/es/search/?q={query}",
    ),
}

# Endpoint de búsqueda pública observado en la web de Lidl.
# No es una API oficial documentada y puede cambiar.
LIDL_SEARCH_URL = os.getenv(
    "LIDL_SEARCH_URL",
    "https://www.lidl.es/q/api/search",
)

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/ld+json,"
        "application/json;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
})

# Si una web responde 403/429, pausamos temporalmente las consultas.
_BLOCKED_UNTIL: dict[str, float] = {}


def _walk(value: Any):
    """Recorre estructuras JSON anidadas."""
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)

    elif isinstance(value, dict):
        yield value

        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk(child)


def _jsonld_documents(html: str):
    """Extrae bloques JSON-LD de una página HTML."""
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
        r'(.*?)</script>',
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
    product_type = obj.get("@type", [])

    if isinstance(product_type, str):
        product_type = [product_type]

    return "Product" in product_type


def _to_price(value):
    """Convierte un precio explícito a float, sin asumir céntimos."""
    if isinstance(value, dict):
        value = (
            value.get("amount")
            or value.get("value")
            or value.get("price")
        )

    if value is None:
        return None

    try:
        price = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None

    return price if price > 0 else None


def _extract_products(
    html: str,
    base_url: str,
    supermarket: str,
    query: str,
    postal_code: str,
):
    """Extrae Product + Offer JSON-LD con precio en euros."""
    seen = set()

    for document in _jsonld_documents(html):
        for product in _walk(document):
            if not _is_product(product):
                continue

            name = str(product.get("name") or "").strip()

            if not name:
                continue

            offers = product.get("offers") or {}

            for offer in _as_list(offers):
                if not isinstance(offer, dict):
                    continue

                currency = str(
                    offer.get("priceCurrency") or "EUR"
                ).upper()

                if currency != "EUR":
                    continue

                raw_price = offer.get("price")

                if raw_price is None:
                    specification = offer.get("priceSpecification")

                    if isinstance(specification, dict):
                        raw_price = specification.get("price")

                price = _to_price(raw_price)

                if price is None:
                    continue

                availability = str(
                    offer.get("availability") or ""
                ).lower()

                if availability and not any(
                    status in availability
                    for status in (
                        "instock",
                        "limitedavailability",
                        "preorder",
                    )
                ):
                    continue

                url = urljoin(
                    base_url,
                    str(
                        offer.get("url")
                        or product.get("url")
                        or base_url
                    ),
                )

                sku = str(
                    product.get("sku")
                    or product.get("gtin13")
                    or product.get("gtin")
                    or url
                )

                identity = (
                    supermarket.lower(),
                    sku,
                    round(price, 4),
                )

                if identity in seen:
                    continue

                seen.add(identity)

                brand = product.get("brand")

                if isinstance(brand, dict):
                    brand = brand.get("name")

                # Solo se conserva el precio unitario si la web lo publica.
                unit_price = None
                unit = None

                unit_prop = (
                    product.get("unitPriceSpecification")
                    or offer.get("unitPriceSpecification")
                )

                if isinstance(unit_prop, dict):
                    try:
                        reference_quantity = (
                            unit_prop.get("referenceQuantity") or {}
                        )

                        quantity_value = (
                            reference_quantity.get("value", 1)
                            if isinstance(reference_quantity, dict)
                            else 1
                        )

                        if float(quantity_value) != 1:
                            raise ValueError(
                                "Referencia no estándar"
                            )

                        unit_price = _to_price(
                            unit_prop.get("price")
                        )

                        unit_raw = str(
                            unit_prop.get("unitCode")
                            or unit_prop.get("unitText")
                            or ""
                        ).lower()

                        unit = {
                            "kgm": "kg",
                            "ltr": "l",
                            "ltq": "l",
                            "c62": "unidad",
                        }.get(unit_raw, unit_raw) or None

                    except (TypeError, ValueError):
                        unit_price = None
                        unit = None

                yield {
                    "supermarket": supermarket,
                    "name": name,
                    "normalized_name": re.sub(
                        r"\s+",
                        " ",
                        re.sub(r"[^a-z0-9]+", " ", name.lower()),
                    ).strip(),
                    "pack_price": price,
                    "price": price,
                    "unit_price": unit_price,
                    "unit": unit,
                    "product_url": url,
                    "brand": str(brand).strip() if brand else None,
                    "source": f"Catálogo web público {supermarket}",
                    "captured_at": date.today().isoformat(),
                    "postal_code": postal_code,
                    "source_product_id": sku,
                    "raw_data": {
                        "query": query,
                        "requested_postal_code": postal_code,
                        "location_scope": (
                            "web_catalog_for_requested_postal_code_unverified"
                        ),
                        "price_observed_at": date.today().isoformat(),
                        "date_estimated": False,
                        "jsonld_offer": True,
                    },
                }


def _extract_lidl_api(
    data: Any,
    query: str,
    postal_code: str,
):
    """
    Extrae resultados de la respuesta JSON de Lidl.

    Se exigen nombre y precio explícitos. No se interpreta un precio
    ausente ni se convierte automáticamente de céntimos a euros.
    """
    seen = set()

    name_keys = (
        "title",
        "name",
        "productName",
        "product_name",
        "displayName",
    )

    price_keys = (
        "price",
        "salesPrice",
        "currentPrice",
        "priceValue",
    )

    id_keys = (
        "id",
        "productId",
        "product_id",
        "sku",
        "code",
    )

    url_keys = (
        "url",
        "productUrl",
        "product_url",
        "link",
    )

    for obj in _walk(data):
        if not isinstance(obj, dict):
            continue

        name = None

        for key in name_keys:
            value = obj.get(key)

            if isinstance(value, str) and value.strip():
                name = value.strip()
                break

        if not name:
            continue

        price = None

        for key in price_keys:
            if key in obj:
                price = _to_price(obj.get(key))

                if price is not None:
                    break

        if price is None:
            continue

        product_id = None

        for key in id_keys:
            if obj.get(key) is not None:
                product_id = str(obj[key])
                break

        raw_url = None

        for key in url_keys:
            if isinstance(obj.get(key), str):
                raw_url = obj[key]
                break

        url = (
            urljoin("https://www.lidl.es", raw_url)
            if raw_url
            else None
        )

        identity = (product_id or name, round(price, 4))

        if identity in seen:
            continue

        seen.add(identity)

        yield {
            "supermarket": "Lidl",
            "name": name,
            "normalized_name": re.sub(
                r"\s+",
                " ",
                re.sub(r"[^a-z0-9]+", " ", name.lower()),
            ).strip(),
            "pack_price": price,
            "price": price,
            "unit_price": None,
            "unit": None,
            "product_url": url,
            "brand": None,
            "source": "Buscador público Lidl (experimental)",
            "captured_at": date.today().isoformat(),
            "postal_code": postal_code,
            "source_product_id": product_id or url or name,
            "raw_data": {
                "query": query,
                "requested_postal_code": postal_code,
                "location_scope": (
                    "web_catalog_for_requested_postal_code_unverified"
                ),
                "price_observed_at": date.today().isoformat(),
                "date_estimated": False,
                "experimental_endpoint": True,
            },
        }


def _search_lidl(query: str, postal_code: str) -> list[dict]:
    """Consulta el buscador público de Lidl, si sigue disponible."""
    supermarket = "Lidl"

    if time.time() < _BLOCKED_UNTIL.get(supermarket, 0):
        log.info("[Lidl] Conector pausado temporalmente.")
        return []

    try:
        response = SESSION.get(
            LIDL_SEARCH_URL,
            params={"query": query},
            timeout=TIMEOUT,
        )

        if response.status_code == 400:
            _BLOCKED_UNTIL[supermarket] = time.time() + 900

            log.warning(
                "[Lidl] HTTP 400. La petición no es válida. "
                "Respuesta: %s",
                response.text[:300],
            )

            return []

        if response.status_code in (403, 429):
            _BLOCKED_UNTIL[supermarket] = time.time() + 900

            log.warning(
                "[Lidl] HTTP %s; se pausa el conector 15 minutos.",
                response.status_code,
            )
            return []

        response.raise_for_status()

        try:
            data = response.json()
        except ValueError:
            log.warning(
                "[Lidl] La respuesta del buscador no es JSON."
            )
            return []

        rows = list(
            _extract_lidl_api(data, query, postal_code)
        )

        log.info(
            "[Lidl] Consulta '%s': %s ofertas verificables",
            query,
            len(rows),
        )

        return rows

    except requests.RequestException as exc:
        log.warning(
            "[Lidl] No se pudo consultar el buscador: %s",
            exc,
        )
        return []


def search_public_catalog(
    query: str,
    stores: list[str],
    postal_code: str,
) -> list[dict]:
    """Consulta los catálogos públicos configurados."""
    results = []

    for supermarket in ("Alcampo", "Carrefour", "Eroski"):
        if supermarket not in stores:
            continue

        if time.time() < _BLOCKED_UNTIL.get(supermarket, 0):
            log.info(
                "[%s] Conector pausado temporalmente.",
                supermarket,
            )
            continue

        template = SEARCH_TEMPLATES[supermarket]

        url = template.format(
            query=quote_plus(query),
            postal_code=quote_plus(postal_code),
        )

        try:
            response = SESSION.get(url, timeout=TIMEOUT)

            if response.status_code in (403, 429):
                _BLOCKED_UNTIL[supermarket] = time.time() + 900

                log.warning(
                    "[%s] HTTP %s; se pausa el conector 15 minutos.",
                    supermarket,
                    response.status_code,
                )
                continue

            response.raise_for_status()

            rows = list(
                _extract_products(
                    response.text,
                    response.url,
                    supermarket,
                    query,
                    postal_code,
                )
            )

            log.info(
                "[%s] Consulta '%s': %s ofertas JSON-LD",
                supermarket,
                query,
                len(rows),
            )

            results.extend(rows)

        except requests.RequestException as exc:
            status = getattr(
                getattr(exc, "response", None),
                "status_code",
                None,
            )

            if status in (403, 429):
                _BLOCKED_UNTIL[supermarket] = time.time() + 900

            log.warning(
                "[%s] No se pudo consultar el catálogo: %s",
                supermarket,
                exc,
            )

        time.sleep(0.35)

    if "Lidl" in stores:
        results.extend(_search_lidl(query, postal_code))

    return results