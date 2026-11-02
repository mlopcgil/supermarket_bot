"""Importa un CSV de precios verificados a PostgreSQL.

CSV obligatorio:
supermarket,name,price,captured_at
Opcionales:
source,product_url,brand,package_size,unit,postal_code,source_key,canonical_key,available

No genera precios. Solo carga filas presentes en el fichero.
"""
import argparse
import csv
import re
import unicodedata
from datetime import date
from decimal import Decimal
from dotenv import load_dotenv
from database import db

load_dotenv()

def normalize(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    parser.add_argument("--source", default="manual_csv")
    args = parser.parse_args()
    count = 0
    with open(args.csv_file, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"supermarket", "name", "price", "captured_at"}
        if not required.issubset(reader.fieldnames or []):
            raise SystemExit(f"CSV debe contener columnas: {', '.join(sorted(required))}")
        with db() as conn, conn.cursor() as cur:
            for row in reader:
                name = (row.get("name") or "").strip()
                supermarket = (row.get("supermarket") or "").strip()
                if not name or not supermarket:
                    continue
                price = Decimal((row.get("price") or "").replace(",", "."))
                if price < 0:
                    raise ValueError(f"Precio negativo no permitido: {row}")
                captured = date.fromisoformat(row["captured_at"].strip())
                cur.execute("INSERT INTO supermarkets(name) VALUES (%s) ON CONFLICT(name) DO NOTHING", (supermarket,))
                cur.execute("SELECT id FROM supermarkets WHERE name=%s", (supermarket,))
                sid = cur.fetchone()[0]
                key = (row.get("source_key") or "").strip() or f"{supermarket}|{normalize(name)}|{row.get('postal_code','')}|{captured.isoformat()}"
                cur.execute("""
                    INSERT INTO products(
                      source_key,name,normalized_name,canonical_key,brand,package_size,unit_price,unit,
                      product_url,source,captured_at,postal_code,supermarket_id,available,raw_data
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(source_key) DO UPDATE SET
                      name=EXCLUDED.name, normalized_name=EXCLUDED.normalized_name,
                      canonical_key=EXCLUDED.canonical_key, unit_price=EXCLUDED.unit_price, unit=EXCLUDED.unit,
                      product_url=EXCLUDED.product_url, captured_at=EXCLUDED.captured_at,
                      available=EXCLUDED.available, raw_data=EXCLUDED.raw_data
                """, (
                    key, name, normalize(name), (row.get("canonical_key") or "").strip() or None, row.get("brand") or None,
                    row.get("package_size") or None, price, row.get("unit") or None,
                    row.get("product_url") or None, row.get("source") or args.source,
                    captured, (row.get("postal_code") or "").strip() or None, sid,
                    (row.get("available") or "true").lower() not in {"0","false","no"},
                    "{}"
                ))
                count += 1
    print(f"Importadas/actualizadas {count} filas de precios. No se han generado precios.")

if __name__ == "__main__":
    main()
