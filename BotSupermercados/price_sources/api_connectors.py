"""Adaptador de precios para MiCestita basado en el dataset público OpenCesta.

No se inventan precios: solo se devuelven filas presentes en la última release.
La cobertura publicada es parcial (Mercadona, DIA y Ahorramás en las zonas
indicadas por OpenCesta); el resto de cadenas se informa como sin cobertura.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
import threading
import unicodedata
import zipfile
from datetime import date
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
# GitHub permite más peticiones autenticadas. El token es opcional.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
if GITHUB_TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

_FRAME = None
_FRAME_AT = 0.0
_FRAME_TAG = None
_LOAD_LOCK = threading.Lock()

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
    """Devuelve (tag, rutas parquet, manifiesto) si la caché local es válida."""
    manifest_path = CACHE_DIR / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        names = manifest.get("parquet_files") or []
        paths = []
        for name in names:
            candidate = Path(name)
            # Compatibilidad con el manifiesto antiguo (solo nombre de archivo).
            path = candidate if candidate.is_absolute() else CACHE_DIR / candidate
            paths.append(path)
        if paths and all(path.is_file() and path.stat().st_size > 0 for path in paths):
            return str(manifest.get("tag") or "cached"), paths, manifest
    except Exception:
        log.exception("[OpenCesta] No se pudo leer el manifiesto de caché.")
    return None


def _use_cached_manifest(cached, reason: str):
    if not cached:
        return None
    tag, paths, manifest = cached
    age = max(0, time.time() - float(manifest.get("downloaded_at", 0)))
    log.warning(
        "[OpenCesta] Se reutiliza la caché %s (edad: %.1f horas). Motivo: %s",
        tag, age / 3600, reason,
    )
    return tag, paths


def _download_latest_release() -> tuple[str, list[Path]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # La comprobación local va ANTES de llamar a GitHub: así no se consume
    # el límite de la API por cada producto de la lista.
    cached = _cached_manifest()
    if cached:
        _, _, manifest = cached
        age = time.time() - float(manifest.get("downloaded_at", 0))
        if 0 <= age < CACHE_TTL:
            log.info("[OpenCesta] Usando caché local vigente (%s).", manifest.get("tag"))
            return cached[0], cached[1]

    try:
        response = SESSION.get(RELEASES_URL, timeout=TIMEOUT)
        response.raise_for_status()
        release = response.json()
        tag = str(release.get("tag_name") or release.get("name") or "latest")
        assets = release.get("assets") or []
        if not assets:
            raise RuntimeError("La última release de OpenCesta no tiene assets.")

        # Separar directorios por versión evita reutilizar accidentalmente un
        # archivo de una release anterior que tenga el mismo nombre.
        safe_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", tag)[:100] or "latest"
        release_dir = CACHE_DIR / safe_tag
        release_dir.mkdir(parents=True, exist_ok=True)

        archives = []
        for asset in assets:
            name, url = asset.get("name", ""), asset.get("browser_download_url")
            low = name.lower()
            if not url or not low.endswith((".parquet", ".zip", ".tar.gz", ".tgz")):
                continue
            target = release_dir / Path(name).name
            if not target.exists() or target.stat().st_size == 0:
                log.info("[OpenCesta] Descargando %s", name)
                tmp = target.with_name(target.name + ".part")
                try:
                    with SESSION.get(url, stream=True, timeout=TIMEOUT) as r:
                        r.raise_for_status()
                        with tmp.open("wb") as f:
                            for chunk in r.iter_content(1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                    tmp.replace(target)
                finally:
                    if tmp.exists():
                        tmp.unlink()
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
                        out = release_dir / f"{archive.stem}_{Path(member.filename).name}"
                        if not out.exists() or out.stat().st_size == 0:
                            with zf.open(member) as src, out.open("wb") as dst:
                                _copy(src, dst)
                        parquet_files.append(out)
            else:
                with tarfile.open(archive, "r:gz") as tf:
                    for member in tf.getmembers():
                        if not member.isfile() or not member.name.lower().endswith(".parquet"):
                            continue
                        src_file = tf.extractfile(member)
                        if src_file is None:
                            continue
                        out = release_dir / f"{archive.stem}_{Path(member.name).name}"
                        if not out.exists() or out.stat().st_size == 0:
                            with src_file, out.open("wb") as dst:
                                _copy(src_file, dst)
                        else:
                            src_file.close()
                        parquet_files.append(out)

        if not parquet_files:
            raise RuntimeError("Los assets de OpenCesta no contienen Parquet reconocible.")

        manifest = {
            "tag": tag,
            "downloaded_at": time.time(),
            "parquet_files": [str(path.relative_to(CACHE_DIR)) for path in parquet_files],
        }
        manifest_path = CACHE_DIR / "manifest.json"
        tmp_manifest = manifest_path.with_suffix(".json.part")
        tmp_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        tmp_manifest.replace(manifest_path)
        log.info("[OpenCesta] Release %s descargada; %s Parquet.", tag, len(parquet_files))
        return tag, parquet_files

    except Exception as exc:
        # Si GitHub limita la API o falla una descarga, no descartamos los
        # datos anteriores: se siguen usando aunque hayan superado el TTL.
        fallback = _use_cached_manifest(cached, str(exc))
        if fallback:
            return fallback
        raise


def _load_dataset(force=False):
    global _FRAME, _FRAME_AT, _FRAME_TAG
    # Evita que varias actualizaciones de Telegram descarguen el dataset a la vez.
    with _LOAD_LOCK:
        if not force and _FRAME is not None and time.time() - _FRAME_AT < CACHE_TTL:
            return _FRAME_TAG, _FRAME

        tag, files = _download_latest_release()
        frames = []
        for path in files:
            try:
                frames.append(pd.read_parquet(path))
            except Exception:
                log.exception("[OpenCesta] Error leyendo %s", path)
        if not frames:
            raise RuntimeError("No se pudo leer ningún Parquet. Instala una versión compatible de pyarrow.")
        frame = pd.concat(frames, ignore_index=True, sort=False)
        frame.columns = [str(c).lower().strip() for c in frame.columns]

        _FRAME = frame
        _FRAME_AT, _FRAME_TAG = time.time(), tag
        log.info("[OpenCesta] Release %s: %s filas", tag, len(_FRAME))
        return tag, _FRAME


_ZONE_CACHE: dict[str, str | None] = {}

def _postal_zone(cp: str) -> str | None:
    """Resuelve el CP con el comando oficial de OpenCesta, sin inferir provincias."""
    if cp in _ZONE_CACHE:
        return _ZONE_CACHE[cp]
    try:
        result = subprocess.run(
            [os.getenv("OPENCESTA_CLI", "opencesta"), "zone", cp],
            capture_output=True, text=True, timeout=12, check=False,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        # Ejemplo documentado: `28001 -> mad3`.
        match = re.search(r"(?:->|→)\s*([a-z0-9_-]+)", output, re.IGNORECASE)
        if result.returncode == 0 and match:
            _ZONE_CACHE[cp] = match.group(1).lower()
            return _ZONE_CACHE[cp]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("No está disponible el CLI de OpenCesta para resolver el CP %s.", cp)
    _ZONE_CACHE[cp] = None
    return None


def _first(row: dict, *keys, default=None):
    for key in keys:
        value = row.get(key)
        if value is not None and not pd.isna(value):
            return value
    return default


def _date(value) -> str:
    try:
        return pd.to_datetime(value).date().isoformat()
    except Exception:
        return date.today().isoformat()


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

        # Zona regional. No guardamos el CP como si el precio fuera específico del domicilio.
        if "zone" in subset.columns:
            zones = set(subset["zone"].dropna().astype(str).str.lower())
            zone = _postal_zone(zip_code)
            if chain == "mercadona":
                if not zone or zone not in zones:
                    log.info("[OpenCesta] Sin zona Mercadona publicada para CP %s", zip_code)
                    continue
                subset = subset[subset["zone"].astype(str).str.lower() == zone]
            elif chain == "ahorramas":
                if not zip_code.startswith("28") or "madrid" not in zones:
                    log.info("[OpenCesta] Ahorramás sin zona publicada para CP %s", zip_code)
                    continue
                subset = subset[subset["zone"].astype(str).str.lower() == "madrid"]
            elif chain == "dia" and "es-default" in zones:
                subset = subset[subset["zone"].astype(str).str.lower() == "es-default"]

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
            price = _first(row, "price", "pack_price", "unit_price")
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            ref_price = _first(row, "reference_price", "price_per_unit")
            ref_format = _first(row, "reference_format", "reference_unit")
            product_url = _first(row, "product_url", "url", "share_url", default="https://github.com/ruvelro/opencesta")
            brand = _first(row, "brand")
            product_id = _first(row, "product_id", "sku", "id")
            results.append({
                "supermarket": store_name,
                "name": name,
                "normalized_name": norm(name),
                "price": price,
                "unit_price": price,
                "unit": "envase",
                "product_url": str(product_url),
                "brand": str(brand) if brand else None,
                "source": f"OpenCesta {tag}",
                "captured_at": _date(_first(row, "captured_at", "date", "snapshot_date")),
                # Dataset por zona/región; no simular que es una tarifa exacta del CP.
                "postal_code": None,
                "source_product_id": str(product_id) if product_id is not None else None,
                "raw_data": {
                    "zone": _first(row, "zone"),
                    "reference_price": ref_price,
                    "reference_format": ref_format,
                    "requested_postal_code": zip_code,
                },
                "_match_score": int(row.get("_score", 0)),
            })
    return results


def fetch_live_prices(query: str, stores: list[str], zip_code: str | None = None) -> list[dict]:
    """Función pública compatible con la integración actual de database.py."""
    if not zip_code or not re.fullmatch(r"\d{5}", str(zip_code).strip()):
        log.warning("[fetch_live_prices] CP ausente/no válido; se omite la consulta.")
        return []
    try:
        log.info("[fetch_live_prices] '%s' | tiendas=%s | CP=%s", query, stores, zip_code)
        rows = search_opencesta(query, stores, str(zip_code).strip())
        log.info("[fetch_live_prices] '%s': %s productos", query, len(rows))
        return rows
    except requests.RequestException:
        log.exception("[OpenCesta] Error HTTP consultando la release.")
    except Exception:
        log.exception("[OpenCesta] No se pudo consultar el dataset.")
    return []


def clear_cache():
    global _FRAME, _FRAME_AT, _FRAME_TAG
    with _LOAD_LOCK:
        _FRAME, _FRAME_AT, _FRAME_TAG = None, 0.0, None
