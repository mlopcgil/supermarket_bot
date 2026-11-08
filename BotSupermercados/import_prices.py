"""Importa precios verificados desde CSV a PostgreSQL. Nunca genera precios.

Columnas obligatorias: supermarket,name,price,captured_at
Opcionales: source,product_url,brand,package_size,unit,unit_price,
postal_code,source_key,canonical_key,available,region_scope

`price` siempre es el precio del envase. `unit_price` solo debe incluirse
cuando la fuente publique expresamente el precio por unidad de medida.
"""
import argparse
import csv
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

# Debe cargarse el .env ANTES de importar database: DATABASE_URL se lee
# al importar ese módulo.
load_dotenv()
from database import db  # noqa: E402


def normalize(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _decimal(value, field, line, allow_zero=True):
    raw = (value or "").strip().replace("€", "").replace(" ", "").replace(",", ".")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Línea {line}: {field} no es un número válido: {value!r}")
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise ValueError(f"Línea {line}: {field} debe ser un número finito no negativo")
    return number


def _bool(value, default=True):
    raw = (value or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "si", "sí", "y"}:
        return True
    if raw in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"Valor booleano no válido: {value!r}")


def import_csv(csv_file, default_source="manual_csv"):
    count = 0
    with open(csv_file, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"supermarket", "name", "price", "captured_at"}
        headers = set(reader.fieldnames or [])
        missing = required - headers
        if missing:
            raise SystemExit("CSV sin columnas obligatorias: " + ", ".join(sorted(missing)))

        # Una transacción: si una fila es inválida, no deja una importación parcial.
        with db() as conn, conn.cursor() as cur:
            for line, row in enumerate(reader, start=2):
                name = (row.get("name") or "").strip()
                supermarket = (row.get("supermarket") or "").strip()
                if not name or not supermarket:
                    raise ValueError(f"Línea {line}: supermarket y name son obligatorios")

                price = _decimal(row.get("price"), "price", line)
                unit_price_raw = (row.get("unit_price") or "").strip()
                unit_price = _decimal(unit_price_raw, "unit_price", line) if unit_price_raw else None
                try:
                    captured = date.fromisoformat((row.get("captured_at") or "").strip())
                except ValueError:
                    raise ValueError(f"Línea {line}: captured_at debe tener formato YYYY-MM-DD")

                postal_code = (row.get("postal_code") or "").strip() or None
                if postal_code and not re.fullmatch(r"\d{5}", postal_code):
                    raise ValueError(f"Línea {line}: postal_code debe tener 5 dígitos")

                source = (row.get("source") or "").strip() or default_source
                canonical_key = (row.get("canonical_key") or "").strip() or None
                supplied_key = (row.get("source_key") or "").strip()
                key = supplied_key or "|".join((
                    supermarket, source, normalize(name),
                    normalize(row.get("brand") or ""),
                    normalize(row.get("package_size") or ""),
                    postal_code or "regional", captured.isoformat()
                ))
                available = _bool(row.get("available"), True)

                cur.execute(
                    "INSERT INTO supermarkets(name) VALUES (%s) ON CONFLICT(name) DO NOTHING",
                    (supermarket,),
                )
                cur.execute("SELECT id FROM supermarkets WHERE name=%s", (supermarket,))
                sid = cur.fetchone()[0]
                region_scope = (row.get("region_scope") or "").strip().lower()
                if region_scope not in {"", "national"}:
                    raise ValueError(f"Línea {line}: region_scope solo admite 'national' o vacío")
                raw_data = {
                    "import_method": "csv",
                    "source_key": key,
                    "unit_price_supplied": unit_price is not None,
                    "region_scope": region_scope or "unspecified",
                    "requested_postal_code": postal_code,
                }
                cur.execute("""
                    INSERT INTO products(
                      source_key,name,normalized_name,canonical_key,brand,package_size,
                      unit_price,pack_price,unit,product_url,source,captured_at,
                      postal_code,supermarket_id,available,raw_data
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(source_key) DO UPDATE SET
                      name=EXCLUDED.name,
                      normalized_name=EXCLUDED.normalized_name,
                      canonical_key=EXCLUDED.canonical_key,
                      brand=EXCLUDED.brand,
                      package_size=EXCLUDED.package_size,
                      unit_price=EXCLUDED.unit_price,
                      pack_price=EXCLUDED.pack_price,
                      unit=EXCLUDED.unit,
                      product_url=EXCLUDED.product_url,
                      source=EXCLUDED.source,
                      captured_at=EXCLUDED.captured_at,
                      postal_code=EXCLUDED.postal_code,
                      supermarket_id=EXCLUDED.supermarket_id,
                      available=EXCLUDED.available,
                      raw_data=EXCLUDED.raw_data
                """, (
                    key, name, normalize(name), canonical_key,
                    (row.get("brand") or "").strip() or None,
                    (row.get("package_size") or "").strip() or None,
                    unit_price, price, (row.get("unit") or "").strip() or None,
                    (row.get("product_url") or "").strip() or None,
                    source, captured, postal_code, sid, available,
                    __import__("json").dumps(raw_data),
                ))
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Importa precios verificados a PostgreSQL; no genera precios.")
    parser.add_argument("csv_file")
    parser.add_argument("--source", default="manual_csv")
    args = parser.parse_args()
    count = import_csv(args.csv_file, args.source)
    print(f"Importadas/actualizadas {count} filas de precios verificados. No se han generado precios.")


if __name__ == "__main__":
    main()
