# Fuentes de precios

- **OpenCesta**: snapshots Parquet públicos. Se consulta únicamente la zona que publica el dataset; en CP de Madrid puede utilizarse una zona Mercadona alternativa y el bot lo avisa.
- **Alcampo y Carrefour**: conector de mejor esfuerzo sobre las páginas públicas de búsqueda. Solo importa ofertas que la página exponga como `Product` + `Offer` en JSON-LD con precio EUR. No es una API oficial ni se garantiza cobertura; no se sortean CAPTCHA, login ni bloqueos.
- **Lidl España**: no tiene compra online de alimentación equiparable a un supermercado online; el conector no inventa catálogo. Los precios de alimentación Lidl se pueden cargar desde un CSV verificado (por ejemplo, precios recogidos del folleto o de tienda).
- **CSV**: `python import_prices.py precios.csv --source "fuente y fecha"`. `price` es el precio del envase; `unit_price` es opcional y solo debe rellenarse si la fuente publica ese precio unitario.

Las tiendas pueden variar precios y disponibilidad según el centro/zona. Los conectores web marcan que la ubicación de entrega no ha podido verificarse automáticamente.
