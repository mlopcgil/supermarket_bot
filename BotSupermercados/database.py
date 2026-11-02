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
            unit TEXT,
            product_url TEXT,
            source TEXT NOT NULL,
            captured_at DATE NOT NULL,
            postal_code CHAR(5),
            supermarket_id INT NOT NULL REFERENCES supermarkets(id),
            available BOOLEAN NOT NULL DEFAULT TRUE,
            raw_data JSONB NOT NULL DEFAULT '{}'::jsonb
        );
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
            key = f"{supermarket}|{p['normalized_name']}|{p.get('postal_code','')}|{p['captured_at']}"
            cur.execute("""
                INSERT INTO products(
                  source_key, name, normalized_name, unit_price, unit, product_url, brand, source, captured_at, postal_code, supermarket_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(source_key) DO UPDATE SET
                  unit_price=EXCLUDED.unit_price, captured_at=EXCLUDED.captured_at
            """, (
                key, p["name"], p["normalized_name"], p["unit_price"], p.get("unit"),
                p.get("product_url"), p.get("brand"), p.get("source"), p["captured_at"],
                p.get("postal_code"), sid
            ))

def get_prices_for_query(items, stores, postal_code=None):
    """Consulta precios en la base de datos o consulta las APIs públicas en tiempo real."""
    # Consultar APIs en vivo primero para obtener los datos más recientes
    try:
        from price_sources.api_connectors import fetch_live_prices
        for query in items:
            live_data = fetch_live_prices(query, stores, postal_code or "28001")
            save_live_prices(live_data)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Error al obtener datos en vivo: {e}")

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT p.name, p.normalized_name, p.canonical_key, p.brand, p.package_size,
                   p.unit_price, p.unit, p.product_url, p.source, p.captured_at,
                   p.postal_code, s.name AS supermarket
            FROM products p JOIN supermarkets s ON s.id=p.supermarket_id
            WHERE s.name = ANY(%s)
              AND p.available=TRUE
              AND p.captured_at >= CURRENT_DATE - 3
              AND (%s IS NULL OR p.postal_code IS NULL OR p.postal_code=%s)
            ORDER BY p.captured_at DESC
        """, (stores, postal_code, postal_code))
        rows = cur.fetchall()
    # Simple normalized term matching. Product equivalence is deliberately conservative.
    import unicodedata, re
    def norm(s):
        s = unicodedata.normalize("NFKD", s.lower())
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9]+", " ", s).strip()
    result = []
    for query in items:
        q = norm(query)
        terms = [t for t in q.split() if len(t) > 2]
        matches = []
        for row in rows:
            n = row["normalized_name"] or norm(row["name"])
            if q == n or (terms and all(t in n for t in terms)):
                item = dict(row)
                item["unit_price"] = float(item["unit_price"]) if item["unit_price"] is not None else None
                matches.append(item)
        # Keep latest price per supermarket/name for this query
        newest = {}
        for item in matches:
            key = (item["supermarket"], item["normalized_name"])
            if key not in newest or item["captured_at"] > newest[key]["captured_at"]:
                newest[key] = item
        # Preferir un grupo explícitamente equivalente (canonical_key). Sin clave,
        # solo se consideran equivalentes nombres normalizados idénticos.
        groups = {}
        for item in newest.values():
            group_key = item.get("canonical_key") or item["normalized_name"]
            groups.setdefault(group_key, []).append(item)
        if groups:
            # Elegir el grupo que cubre más supermercados; desempate por frescura.
            selected = sorted(
                groups.values(),
                key=lambda group: (
                    len({x["supermarket"] for x in group}),
                    max(x["captured_at"] for x in group)
                ),
                reverse=True
            )[0]
        else:
            selected = []
        result.append({"query": query, "prices": selected})
    return result

def save_session(telegram_id, items, stores, result):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shopping_sessions(telegram_id,items,supermarkets,result) VALUES (%s,%s,%s,%s)",
            (telegram_id, json.dumps(items), json.dumps(stores), json.dumps(result, default=str))
        )
