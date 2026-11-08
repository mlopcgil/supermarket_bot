import os
import json
from datetime import datetime, timezone
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Falta DATABASE_URL en el entorno/.env")

@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL, sslmode=os.getenv("DB_SSLMODE", "require"))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS grocery_users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            postal_code CHAR(5),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS supermarkets (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS user_supermarkets (
            telegram_id BIGINT NOT NULL REFERENCES grocery_users(telegram_id) ON DELETE CASCADE,
            supermarket_id INT NOT NULL REFERENCES supermarkets(id),
            PRIMARY KEY (telegram_id, supermarket_id)
        );
        CREATE TABLE IF NOT EXISTS shopping_sessions (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES grocery_users(telegram_id) ON DELETE CASCADE,
            items JSONB NOT NULL,
            supermarkets JSONB NOT NULL,
            result JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS products (
            id BIGSERIAL PRIMARY KEY,
            source_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            canonical_key TEXT,
            brand TEXT,
            package_size TEXT,
            unit_price NUMERIC(12,4),
            pack_price NUMERIC(12,4),
            unit TEXT,
            product_url TEXT,
            source TEXT NOT NULL,
            captured_at DATE NOT NULL,
            postal_code CHAR(5),
            supermarket_id INT NOT NULL REFERENCES supermarkets(id),
            available BOOLEAN NOT NULL DEFAULT TRUE,
            raw_data JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        ALTER TABLE products ADD COLUMN IF NOT EXISTS pack_price NUMERIC(12,4);
        CREATE INDEX IF NOT EXISTS idx_products_lookup ON products(normalized_name);
        CREATE INDEX IF NOT EXISTS idx_products_store_date ON products(supermarket_id, captured_at DESC);
        """)
        cur.executemany(
            "INSERT INTO supermarkets(name) VALUES (%s) ON CONFLICT(name) DO NOTHING",
            [(n,) for n in ["Mercadona", "Carrefour", "Lidl", "DIA", "Alcampo", "Aldi", "Eroski", "Ahorramás"]]
        )

def ensure_user(telegram_id, username, first_name):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO grocery_users(telegram_id, username, first_name)
            VALUES (%s,%s,%s)
            ON CONFLICT(telegram_id) DO UPDATE
            SET username=EXCLUDED.username, first_name=EXCLUDED.first_name
        """, (telegram_id, username, first_name))

def get_supermarkets():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM supermarkets WHERE active=TRUE ORDER BY name")
        return [r[0] for r in cur.fetchall()]

def get_user_supermarkets(telegram_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT s.name FROM user_supermarkets us
            JOIN supermarkets s ON s.id=us.supermarket_id
            WHERE us.telegram_id=%s ORDER BY s.name
        """, (telegram_id,))
        return [r[0] for r in cur.fetchall()]

def set_user_supermarkets(telegram_id, names):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM user_supermarkets WHERE telegram_id=%s", (telegram_id,))
        if names:
            cur.execute("SELECT id,name FROM supermarkets WHERE name = ANY(%s)", (names,))
            rows = cur.fetchall()
            cur.executemany(
                "INSERT INTO user_supermarkets(telegram_id,supermarket_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                [(telegram_id, row[0]) for row in rows]
            )

def set_postal_code(telegram_id, postal_code):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE grocery_users SET postal_code=%s WHERE telegram_id=%s", (postal_code, telegram_id))

def get_postal_code(telegram_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT postal_code FROM grocery_users WHERE telegram_id=%s", (telegram_id,))
        row = cur.fetchone()
        return row[0].strip() if row and row[0] else None

def save_live_prices(products):
    if not products:
        return
    with db() as conn, conn.cursor() as cur:
        for p in products:
            supermarket = p["supermarket"]
            cur.execute("INSERT INTO supermarkets(name) VALUES (%s) ON CONFLICT(name) DO NOTHING", (supermarket,))
            cur.execute("SELECT id FROM supermarkets WHERE name=%s", (supermarket,))
            sid = cur.fetchone()[0]
            # El identificador de origen evita colapsar dos formatos distintos
            # que compartan nombre normalizado y fecha.
            identity = p.get("source_product_id") or p.get("product_url") or p["normalized_name"]
            zone = (p.get("raw_data") or {}).get("zone") or "regional"
            key = f"{supermarket}|{identity}|{zone}|{p.get('postal_code') or 'regional'}|{p['captured_at']}"
            cur.execute("""
                INSERT INTO products(
                  source_key, name, normalized_name, canonical_key, package_size,
                  unit_price, pack_price, unit, product_url, brand, source,
                  captured_at, postal_code, supermarket_id, raw_data
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT(source_key) DO UPDATE SET
                  name=EXCLUDED.name,
                  normalized_name=EXCLUDED.normalized_name,
                  canonical_key=EXCLUDED.canonical_key,
                  package_size=EXCLUDED.package_size,
                  unit_price=EXCLUDED.unit_price,
                  pack_price=EXCLUDED.pack_price,
                  unit=EXCLUDED.unit,
                  product_url=EXCLUDED.product_url,
                  brand=EXCLUDED.brand,
                  source=EXCLUDED.source,
                  captured_at=EXCLUDED.captured_at,
                  postal_code=EXCLUDED.postal_code,
                  raw_data=EXCLUDED.raw_data,
                  available=TRUE
            """, (
                key, p["name"], p["normalized_name"], p.get("canonical_key"),
                p.get("package_size"), p.get("unit_price"),
                p.get("pack_price", p.get("price")), p.get("unit"), p.get("product_url"),
                p.get("brand"), p.get("source"), p["captured_at"],
                p.get("postal_code"), sid, json.dumps(p.get("raw_data") or {}, default=str)
            ))

def get_prices_for_query(items, stores, postal_code=None):
    """Consulta conectores y recupera solo precios recientes del CP solicitado.

    Los registros regionales de OpenCesta se aceptan únicamente si se
    descargaron para ese mismo CP; los datos nacionales deben marcarse de forma
    explícita con raw_data.region_scope='national'.
    """
    if not postal_code or not str(postal_code).strip().isdigit() or len(str(postal_code).strip()) != 5:
        return [{"query": item, "prices": []} for item in items]
    postal_code = str(postal_code).strip()

    try:
        from price_sources.api_connectors import fetch_live_prices
        for query in items:
            live_data = fetch_live_prices(query, stores, postal_code)
            if live_data:
                save_live_prices(live_data)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Error al obtener datos en vivo")

    max_age_days = int(os.getenv("PRICE_MAX_AGE_DAYS", "3"))
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT p.name, p.normalized_name, p.canonical_key, p.brand, p.package_size,
                   p.unit_price, p.pack_price, p.unit, p.product_url, p.source, p.captured_at,
                   p.postal_code, p.raw_data, s.name AS supermarket
            FROM products p JOIN supermarkets s ON s.id=p.supermarket_id
            WHERE s.name = ANY(%s)
              AND p.available=TRUE
              AND p.captured_at >= CURRENT_DATE - %s
              AND (
                    p.postal_code=%s
                    OR (p.postal_code IS NULL AND (
                        p.raw_data->>'requested_postal_code'=%s
                        OR p.raw_data->>'region_scope'='national'
                    ))
              )
            ORDER BY p.captured_at DESC
        """, (stores, max_age_days, postal_code, postal_code))
        rows = cur.fetchall()

    import unicodedata, re
    def _norm(value):
        value = unicodedata.normalize("NFKD", (value or "").lower())
        value = "".join(c for c in value if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    result = []
    for query in items:
        q = _norm(query)
        terms = [t for t in q.split() if len(t) > 1]
        matches = []
        for row in rows:
            name_norm = row["normalized_name"] or _norm(row["name"])
            name_terms = set(name_norm.split())
            score = sum(1 for term in terms if term in name_terms or (len(term) > 4 and term[:-1] in name_terms))
            if q and q in name_norm:
                score += 5
            if score > 0 and (len(terms) <= 1 or score >= max(1, len(terms) - 1)):
                item = dict(row)
                item["unit_price"] = float(item["unit_price"]) if item["unit_price"] is not None else None
                item["pack_price"] = float(item["pack_price"]) if item["pack_price"] is not None else None
                item["_match_score"] = score
                matches.append(item)

        best_by_store = {}
        for item in matches:
            store_name = item["supermarket"]
            current = best_by_store.get(store_name)
            item_date = item["captured_at"]
            current_date = current["captured_at"] if current else None
            if current is None or (item["_match_score"], item_date) > (current["_match_score"], current_date):
                best_by_store[store_name] = item
        result.append({"query": query, "prices": list(best_by_store.values())})
    return result

def save_session(telegram_id, items, stores, result):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shopping_sessions(telegram_id,items,supermarkets,result) VALUES (%s,%s,%s,%s)",
            (telegram_id, json.dumps(items), json.dumps(stores), json.dumps(result, default=str))
        )
