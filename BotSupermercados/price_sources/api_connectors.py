"""Agregador de fuentes públicas para MiCestita.

No se inventan precios: OpenCesta aporta snapshots publicados y Alcampo /
Carrefour se consultan mediante sus páginas públicas cuando exponen ofertas
JSON-LD. La cobertura es parcial; Lidl alimentación se deja para CSV verificado.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
import unicodedata
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

log = logging.getLogger(__name__)
RELEASES_URL = "https://api.github.com/repos/ruvelro/opencesta/releases/latest"
CACHE_DIR = Path(os.getenv("OPENCESTA_CACHE_DIR", "/tmp/micestita_opencesta"))
CACHE_TTL = int(os.getenv("OPENCESTA_CACHE_TTL_SECONDS", "21600"))
TIMEOUT = int(os.getenv("PRICE_HTTP_TIMEOUT", "30"))
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": os.getenv("PRICE_USER_AGENT", "MiCestita/1.0 price-comparison-bot"),
    "Accept": "application/vnd.github+json, application/octet-stream, */*",
})
# GitHub permite 60 peticiones/hora sin autenticar y muchas más con token.
# Render debe tener GITHUB_TOKEN o GH_TOKEN con el token completo.
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
if GITHUB_TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    log.info("[OpenCesta] Token de GitHub configurado (valor oculto).")
else:
    log.warning("[OpenCesta] No hay GITHUB_TOKEN/GH_TOKEN; se usará el límite anónimo de GitHub.")
_FRAME = None
_FRAME_AT = 0.0
_FRAME_TAG = None
_LOAD_FAILURE_AT = 0.0
_LOAD_FAILURE_MESSAGE = None

STORE_TO_CHAIN = {
    "mercadona": "mercadona", "dia": "dia", "ahorramas": "ahorramas",
    "ahorramás": "ahorramas", "carrefour": "carrefour", "alcampo": "alcampo",
    "lidl": "lidl", "aldi": "aldi", "eroski": "eroski",
}
CHAIN_TO_STORE = {"mercadona": "Mercadona", "dia": "DIA", "ahorramas": "Ahorramás"}


def norm(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _copy(src, dst):
    shutil.copyfileobj(src, dst)


def _cached_manifest():
    """Devuelve la caché local aunque haya expirado, si sus Parquet existen."""
    manifest_path = CACHE_DIR / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # La versión 2 conserva la identidad de cada Parquet dentro del ZIP/TAR.
        # Los manifiestos antiguos podían apuntar varias veces al mismo archivo.
        if int(manifest.get("cache_format", 0)) < 2:
            return None
        names = manifest.get("parquet_files") or []
        paths = [CACHE_DIR / name for name in names]
        if paths and all(path.exists() and path.stat().st_size > 0 for path in paths):
            return str(manifest.get("tag") or "cached"), paths, float(manifest.get("downloaded_at", 0))
    except Exception:
        log.exception("[OpenCesta] No se pudo leer el manifiesto de caché.")
    return None


def _archive_member_path(archive: Path, member_name: str) -> Path:
    """Genera un nombre único aunque varios Parquet se llamen igual.

    OpenCesta puede publicar particiones como
    chain=mercadona/zone=mad1/date=.../prices.parquet y
    chain=dia/zone=es-default/date=.../prices.parquet.
    No se debe aplanar solo al basename porque ambas rutas colisionan.
    """
    digest = hashlib.sha256(member_name.encode("utf-8")).hexdigest()[:12]
    basename = Path(member_name).name or "data.parquet"
    return CACHE_DIR / f"{archive.stem}_{digest}_{basename}"


def _download_latest_release() -> tuple[str, list[Path]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cached_manifest()
    # No consultar GitHub en cada producto: reutilizar la release durante el TTL.
    if cached and time.time() - cached[2] < CACHE_TTL:
        log.info("[OpenCesta] Usando caché %s (edad %.1f h).", cached[0], (time.time() - cached[2]) / 3600)
        return cached[0], cached[1]

    try:
        response = SESSION.get(RELEASES_URL, timeout=TIMEOUT)
        if response.status_code == 403 and "rate limit" in response.text.lower():
            remaining = response.headers.get("X-RateLimit-Remaining", "desconocido")
            reset = response.headers.get("X-RateLimit-Reset", "desconocido")
            raise RuntimeError(
                f"GitHub ha agotado el límite de API (remaining={remaining}, reset={reset}). "
                "Configura un GITHUB_TOKEN válido en Render."
            )
        response.raise_for_status()
        release = response.json()
    except Exception as exc:
        if cached:
            log.warning("[OpenCesta] No se pudo actualizar la release (%s). Se usa caché %s.", exc, cached[0])
            return cached[0], cached[1]
        raise

    tag = str(release.get("tag_name") or release.get("name") or "latest")
    # Si el tag no ha cambiado, no descargar otra vez los mismos Parquet.
    if cached and cached[0] == tag:
        manifest_path = CACHE_DIR / "manifest.json"
        manifest_path.write_text(json.dumps({
            "cache_format": 2,
            "tag": tag, "downloaded_at": time.time(),
            "parquet_files": [p.name for p in cached[1]],
        }), encoding="utf-8")
        return tag, cached[1]

    assets = release.get("assets") or []
    if not assets:
        raise RuntimeError("La última release de OpenCesta no tiene assets.")
    archives = []
    for asset in assets:
        name, url = asset.get("name", ""), asset.get("browser_download_url")
        low = name.lower()
        if not url or not low.endswith((".parquet", ".zip", ".tar.gz", ".tgz")):
            continue
        target = CACHE_DIR / name
        if not target.exists() or target.stat().st_size == 0:
            log.info("[OpenCesta] Descargando %s", name)
            with SESSION.get(url, stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                tmp = target.with_name(target.name + ".part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                tmp.replace(target)
        archives.append(target)

    parquet_files = []
    for archive in archives:
        low = archive.name.lower()
        if low.endswith(".parquet"):
            parquet_files.append(archive)
        elif low.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for member in zf.infolist():
                    if member.is_dir() or not member.filename.lower().endswith(".parquet"):
                        continue
                    out = _archive_member_path(archive, member.filename)
                    if not out.exists() or out.stat().st_size == 0:
                        with zf.open(member) as src, out.open("wb") as dst:
                            _copy(src, dst)
                    parquet_files.append(out)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                for member in tf.getmembers():
                    if not member.isfile() or not member.name.lower().endswith(".parquet"):
                        continue
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    out = _archive_member_path(archive, member.name)
                    if not out.exists() or out.stat().st_size == 0:
                        with src, out.open("wb") as dst:
                            _copy(src, dst)
                    parquet_files.append(out)
    if not parquet_files:
        raise RuntimeError("Los assets de OpenCesta no contienen Parquet reconocible.")
    manifest_path = CACHE_DIR / "manifest.json"
    manifest_path.write_text(json.dumps({
        "cache_format": 2,
        "tag": tag, "downloaded_at": time.time(),
        "parquet_files": [p.name for p in parquet_files],
    }), encoding="utf-8")
    return tag, parquet_files


def _diagnose_catalog_structure(tag: str, files: list[Path], frames: list[pd.DataFrame], df: pd.DataFrame) -> None:
    """Registra metadatos del catálogo para diagnosticar cobertura, sin datos sensibles."""
    log.info("[OpenCesta][Diagnóstico] Release=%s | archivos_parquet=%d | filas_totales=%d", tag, len(files), len(df))
    for path, frame in zip(files, frames):
        cols = [str(c).lower().strip() for c in frame.columns]
        log.info(
            "[OpenCesta][Diagnóstico] archivo=%s | filas=%d | columnas=%s",
            path.name, len(frame), cols[:50],
        )
        original_chain = next((c for c in frame.columns if str(c).lower().strip() == "chain"), None)
        if original_chain is not None:
            counts = frame[original_chain].fillna("<NULL>").astype(str).value_counts().head(25).to_dict()
            log.info("[OpenCesta][Diagnóstico] archivo=%s | valores_chain=%s", path.name, counts)

    if "chain" not in df.columns:
        log.warning("[OpenCesta][Diagnóstico] No existe columna chain. Columnas disponibles=%s", list(df.columns))
        return

    chain_values = df["chain"].fillna("<NULL>").astype(str).value_counts().head(40).to_dict()
    log.info("[OpenCesta][Diagnóstico] distribución_chain=%s", chain_values)
    normalized = df["chain"].fillna("").astype(str).map(norm)
    mercadona_mask = normalized.str.contains("mercadona", regex=False)
    mercadona_rows = df.loc[mercadona_mask]
    log.info("[OpenCesta][Diagnóstico] filas_chain_mercadona=%d", len(mercadona_rows))
    if len(mercadona_rows):
        if "zone" in mercadona_rows.columns:
            zone_counts = mercadona_rows["zone"].fillna("<NULL>").astype(str).value_counts().head(40).to_dict()
            log.info("[OpenCesta][Diagnóstico] zonas_chain_mercadona=%s", zone_counts)
        else:
            log.warning("[OpenCesta][Diagnóstico] Hay filas Mercadona pero no existe columna zone.")
    elif len(df):
        log.warning(
            "[OpenCesta][Diagnóstico] No hay valores chain que contengan 'mercadona'. "
            "Revisar distribución_chain y los archivos anteriores."
        )
    if "zone" in df.columns:
        all_zones = df["zone"].fillna("<NULL>").astype(str).value_counts().head(40).to_dict()
        log.info("[OpenCesta][Diagnóstico] distribución_global_zone=%s", all_zones)
    else:
        log.warning("[OpenCesta][Diagnóstico] No existe columna zone en el dataset agregado.")


def _load_dataset(force=False):
    global _FRAME, _FRAME_AT, _FRAME_TAG, _LOAD_FAILURE_AT, _LOAD_FAILURE_MESSAGE
    if not force and _FRAME is not None and time.time() - _FRAME_AT < CACHE_TTL:
        return _FRAME_TAG, _FRAME
    # Si la API falla y no hay caché, no repetir la misma petición por cada artículo.
    if not force and _LOAD_FAILURE_AT and time.time() - _LOAD_FAILURE_AT < 600:
        raise RuntimeError(_LOAD_FAILURE_MESSAGE or "OpenCesta temporalmente no disponible; reintento en 10 minutos.")
    try:
        tag, files = _download_latest_release()
        frames = []
        valid_files = []
        for path in files:
            try:
                frame = pd.read_parquet(path)
                frames.append(frame)
                valid_files.append(path)
            except Exception:
                log.exception("[OpenCesta] Error leyendo %s", path)
        if not frames:
            raise RuntimeError("No se pudo leer ningún Parquet. Instala pyarrow.")
        _FRAME = pd.concat(frames, ignore_index=True, sort=False)
        _FRAME.columns = [str(c).lower().strip() for c in _FRAME.columns]
        _diagnose_catalog_structure(tag, valid_files, frames, _FRAME)
        _FRAME_AT, _FRAME_TAG = time.time(), tag
        _LOAD_FAILURE_AT, _LOAD_FAILURE_MESSAGE = 0.0, None
        log.info("[OpenCesta] Release %s: %s filas", tag, len(_FRAME))
        return tag, _FRAME
    except Exception as exc:
        _LOAD_FAILURE_AT, _LOAD_FAILURE_MESSAGE = time.time(), str(exc)
        log.exception("[OpenCesta] Error cargando dataset; se conserva la caché disponible.")
        raise


_ZONE_CACHE: dict[str, str | None] = {}

def _postal_zone(cp: str) -> str | None:
    """Resuelve el CP con el comando oficial de OpenCesta, sin inferir provincias."""
    if cp in _ZONE_CACHE:
        return _ZONE_CACHE[cp]
    executable = os.getenv("OPENCESTA_CLI", "opencesta")
    try:
        resolved_executable = shutil.which(executable) or executable
        result = subprocess.run(
            [executable, "zone", cp],
            capture_output=True, text=True, timeout=12, check=False,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        output = stdout + "\n" + stderr
        # Ejemplo documentado: `28001 -> mad3`.
        match = re.search(r"(?:->|→)\s*([a-z0-9_-]+)", output, re.IGNORECASE)
        if result.returncode == 0 and match:
            _ZONE_CACHE[cp] = match.group(1).lower()
            log.info(
                "[OpenCesta][Diagnóstico zona] ejecutable=%s | CP=%s | returncode=%s | zona=%s | stdout=%r",
                resolved_executable, cp, result.returncode, _ZONE_CACHE[cp], stdout[:500],
            )
            return _ZONE_CACHE[cp]
        log.warning(
            "[OpenCesta][Diagnóstico zona] No se pudo resolver CP=%s | ejecutable=%s | "
            "returncode=%s | stdout=%r | stderr=%r",
            cp, resolved_executable, result.returncode, stdout[:500], stderr[:500],
        )
    except FileNotFoundError as exc:
        log.warning("[OpenCesta][Diagnóstico zona] CLI no encontrado (%s): %s", executable, exc)
    except subprocess.TimeoutExpired:
        log.warning("[OpenCesta][Diagnóstico zona] Timeout resolviendo CP=%s con %s.", cp, executable)
    _ZONE_CACHE[cp] = None
    return None


def _first(row: dict, *keys, default=None):
    for key in keys:
        value = row.get(key)
        if value is not None and not pd.isna(value):
            return value
    return default


def _date(value, release_tag: str | None = None) -> tuple[str, bool]:
    """Devuelve fecha y marca si hubo que estimarla.

    Se usa primero la fecha del registro, después la fecha incluida en el
    identificador de release y, si tampoco existe, la fecha actual.
    """
    if value is not None and not (isinstance(value, str) and not value.strip()):
        try:
            parsed = pd.to_datetime(value, errors="raise", utc=True)
            if not pd.isna(parsed):
                return parsed.date().isoformat(), False
        except Exception:
            pass
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", str(release_tag or ""))
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat(), True
        except ValueError:
            pass
    return date.today().isoformat(), True


def _madrid_fallback_zone(zones: set[str], requested_zone: str | None) -> str | None:
    """Elige una zona de Madrid publicada cuando no existe la zona exacta.

    Si el resolver devuelve madN, se aproxima a la zona publicada con sufijo
    numérico más cercano. El número identifica una zona de OpenCesta, no una
    distancia geográfica; por eso siempre se avisa de que es aproximada.
    """
    candidates = sorted(z for z in zones if z == "madrid" or z.startswith("mad"))
    if not candidates:
        return None
    if requested_zone in candidates:
        return requested_zone
    requested_match = re.fullmatch(r"mad(\d+)", requested_zone or "")
    numbered = [z for z in candidates if re.fullmatch(r"mad\d+", z)]
    if requested_match and numbered:
        target = int(requested_match.group(1))
        return min(numbered, key=lambda z: (abs(int(z[3:]) - target), z))
    return numbered[0] if numbered else candidates[0]


def _reference_unit(value) -> str | None:
    """Normaliza unidades de precio de referencia para comparar magnitudes equivalentes."""
    if value is None or pd.isna(value):
        return None
    raw = norm(value).replace(" ", "")
    if any(token in raw for token in ("kg", "kilo", "kilogramo")):
        return "kg"
    if any(token in raw for token in ("100g", "100gram")):
        return "100g"
    if any(token in raw for token in ("litro", "litros", "/l", "eur/l", "€/l")):
        return "l"
    if any(token in raw for token in ("unidad", "ud", "/u", "unit")):
        return "unidad"
    return None


def _positive_number(value):
    try:
        number = float(str(value).replace(",", "."))
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _score(query: str, name: str) -> int:
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0
    if q in n:
        return 100 + len(q.split())
    terms, name_terms = [x for x in q.split() if len(x) > 1], set(n.split())
    score = 0
    for term in terms:
        if term in name_terms or (len(term) > 4 and term[:-1] in name_terms):
            score += 1
    return score


def search_opencesta(query: str, stores: list[str], zip_code: str) -> list[dict]:
    tag, df = _load_dataset()
    if "chain" not in df.columns:
        raise RuntimeError("El Parquet de OpenCesta no contiene la columna 'chain'.")
    name_col = next((c for c in ("display_name", "name", "product_name", "title") if c in df.columns), None)
    if not name_col:
        raise RuntimeError("El Parquet no contiene una columna de nombre de producto reconocible.")

    results = []
    for requested in stores:
        chain = STORE_TO_CHAIN.get(norm(requested).replace(" ", ""))
        if not chain:
            continue
        subset = df[df["chain"].astype(str).str.lower() == chain].copy()
        if subset.empty:
            continue

        # Zona regional. Para CP de Madrid se permite una zona Mercadona
        # madrileña alternativa si OpenCesta no publica la zona exacta.
        selected_zone = None
        location_fallback = False
        if "zone" in subset.columns:
            zones = set(subset["zone"].dropna().astype(str).str.lower())
            zone = _postal_zone(zip_code)
            if chain == "mercadona":
                if zone and zone in zones:
                    selected_zone = zone
                elif zip_code.startswith("28"):
                    selected_zone = _madrid_fallback_zone(zones, zone)
                    location_fallback = selected_zone is not None
                    if location_fallback:
                        log.warning(
                            "[OpenCesta] CP %s sin zona exacta (%s); usando zona aproximada %s",
                            zip_code, zone or "no resuelta", selected_zone,
                        )
                if not selected_zone:
                    log.warning(
                        "[OpenCesta] Mercadona no disponible para CP %s: zona_resuelta=%r; zonas_release=%s",
                        zip_code, zone, sorted(zones),
                    )
                    continue
                log.info("[OpenCesta] Mercadona CP %s -> zona %s (aproximada=%s)", zip_code, selected_zone, location_fallback)
                subset = subset[subset["zone"].astype(str).str.lower() == selected_zone]
            elif chain == "ahorramas":
                if not zip_code.startswith("28") or "madrid" not in zones:
                    log.info("[OpenCesta] Ahorramás sin zona publicada para CP %s", zip_code)
                    continue
                selected_zone = "madrid"
                subset = subset[subset["zone"].astype(str).str.lower() == selected_zone]
            elif chain == "dia" and "es-default" in zones:
                selected_zone = "es-default"
                subset = subset[subset["zone"].astype(str).str.lower() == selected_zone]

        subset["_score"] = subset[name_col].astype(str).map(lambda x: _score(query, x))
        # Evitar falsos positivos: exigir al menos una coincidencia textual.
        subset = subset[subset["_score"] > 0]
        if subset.empty:
            continue
        sort_cols = ["_score"] + (["captured_at"] if "captured_at" in subset.columns else [])
        subset = subset.sort_values(sort_cols, ascending=[False] + ([False] if len(sort_cols) > 1 else [])).head(15)

        store_name = CHAIN_TO_STORE.get(chain, requested)
        for row in subset.to_dict("records"):
            name = str(_first(row, name_col, default=""))
            pack_price = _positive_number(_first(row, "price", "pack_price", "unit_price"))
            if pack_price is None:
                continue
            ref_price = _positive_number(_first(row, "reference_price", "price_per_unit"))
            ref_format = _first(row, "reference_format", "reference_unit")
            comparison_unit = _reference_unit(ref_format) if ref_price is not None else None
            # unit_price solo contiene un precio comparable por unidad de medida.
            # Si OpenCesta no publica referencia, se conserva el precio del envase
            # pero no se usa para proclamar un ganador entre formatos desconocidos.
            comparable_price = ref_price if comparison_unit else None
            captured_at, date_estimated = _date(
                _first(row, "captured_at", "date", "snapshot_date"), tag
            )
            product_url = _first(row, "product_url", "url", "share_url", default="https://github.com/ruvelro/opencesta")
            brand = _first(row, "brand")
            product_id = _first(row, "product_id", "sku", "id")
            results.append({
                "supermarket": store_name,
                "name": name,
                "normalized_name": norm(name),
                "price": pack_price,
                "pack_price": pack_price,
                "package_size": str(_first(row, "package_size", "pack_size", "size", "net_content")) if _first(row, "package_size", "pack_size", "size", "net_content") is not None else None,
                "unit_price": comparable_price,
                "unit": comparison_unit,
                "product_url": str(product_url),
                "brand": str(brand) if brand else None,
                "source": f"OpenCesta {tag}",
                "captured_at": captured_at,
                # Dataset por zona/región; no simular que es una tarifa exacta del CP.
                "postal_code": None,
                "source_product_id": str(product_id) if product_id is not None else None,
                "raw_data": {
                    "zone": selected_zone or _first(row, "zone"),
                    "location_fallback": location_fallback,
                    "location_label": "Madrid (zona aproximada)" if location_fallback else None,
                    "date_estimated": date_estimated,
                    "reference_price": ref_price,
                    "reference_format": ref_format,
                    "comparison_unit": comparison_unit,
                    "requested_postal_code": zip_code,
                },
                "_match_score": int(row.get("_score", 0)),
            })
    return results


def fetch_live_prices(query: str, stores: list[str], zip_code: str | None = None) -> list[dict]:
    """Consulta fuentes disponibles sin fabricar precios.

    OpenCesta cubre sus cadenas/zona publicadas. Alcampo y Carrefour se
    consultan mediante sus páginas públicas cuando exponen Product/Offer JSON-LD.
    Lidl queda fuera del catálogo automático de alimentación en España.
    """
    if not zip_code or not re.fullmatch(r"\d{5}", str(zip_code).strip()):
        log.warning("[fetch_live_prices] CP ausente/no válido; se omite la consulta.")
        return []
    zip_code = str(zip_code).strip()
    results = []

    opencesta_stores = [s for s in stores if norm(s).replace(" ", "") in STORE_TO_CHAIN]
    if opencesta_stores:
        try:
            log.info("[OpenCesta] '%s' | tiendas=%s | CP=%s", query, opencesta_stores, zip_code)
            results.extend(search_opencesta(query, opencesta_stores, zip_code))
        except requests.RequestException as exc:
            log.error("[OpenCesta] Error HTTP consultando la release: %s", exc)
        except Exception as exc:
            log.error("[OpenCesta] No se pudo consultar el dataset: %s", exc)

    web_stores = [s for s in stores if norm(s) in {"alcampo", "carrefour", "lidl"}]
    if web_stores:
        try:
            from price_sources.web_catalog import search_public_catalog
            results.extend(search_public_catalog(query, web_stores, zip_code))
        except Exception:
            log.exception("[Catálogos web] Error consultando los comercios.")

    log.info("[fetch_live_prices] '%s': %s precios verificados recuperados", query, len(results))
    return results


def clear_cache():
    global _FRAME, _FRAME_AT, _FRAME_TAG
    _FRAME, _FRAME_AT, _FRAME_TAG = None, 0.0, None
