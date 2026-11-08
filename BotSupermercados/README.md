# Bot de comparación de precios de supermercados

Bot independiente basado en la estructura del bot de referencia: `pyTelegramBotAPI`, Flask webhook y PostgreSQL. No reutiliza ni modifica sus tablas de ropa.

## Qué incluye esta primera versión

- Preferencias de supermercados por usuario guardadas en PostgreSQL.
- Añadir supermercados a una compra concreta sin alterar las preferencias permanentes.
- Lista de compra separada por comas, punto y coma o saltos de línea.
- Comparación por producto, ahorro entre el precio más alto y más bajo y coste total por cadena.
- No calcula un total ganador si faltan precios de uno o varios productos.
- Solo muestra precios importados con fecha reciente (últimos 3 días).
- Importador CSV de precios verificados.
- Código postal obligatorio antes de comparar precios.
- Conector con la última release pública de OpenCesta (Parquet), con caché local.
- Conectores web de mejor esfuerzo para Alcampo y Carrefour: solo aceptan precios publicados en JSON-LD `Product` + `Offer`; no sortean bloqueos y pueden devolver cero resultados si cambia la web.
- Lidl España no dispone de una tienda online de alimentación comparable; los precios de alimentación Lidl se importan desde CSV verificados (por ejemplo, folleto o comprobación en tienda).
- Aviso de zona cuando Mercadona utiliza una zona madrileña alternativa. El sufijo `madN` no representa distancia geográfica, por lo que el resultado se marca como aproximado.
- Filtrado de registros por código postal para no reutilizar accidentalmente un precio regional recogido para otro CP.

## Limitación importante sobre los datos

El conector automático descarga la última release de OpenCesta desde GitHub y consulta su dataset Parquet. Según la documentación pública consultada, actualmente publica Mercadona en zonas concretas, DIA y Ahorramás; no ofrece todavía cobertura completa de Carrefour, Lidl, Aldi, Eroski y Alcampo. Para esas tiendas el bot indicará que no hay datos, en lugar de inventar precios. La disponibilidad exacta depende de la zona publicada y del último snapshot.

Fuentes públicas investigadas como posibles siguientes pasos:
- CholloSúper publica ficheros abiertos bajo CC BY 4.0, pero su descarga abierta incluye una cesta fija, índice, frescura y bajadas; no equivale a un catálogo completo para responder cualquier lista. Atribución requerida: `Fuente: CholloSúper (chollosuper.com), consultado el [fecha]`.
- OpenCesta publica snapshots Parquet diarios. El código resuelve el código postal mediante su CLI `opencesta zone`; si la zona resuelta no está en la release, no consulta una zona distinta.
- El código postal importa: los precios de algunas cadenas cambian por tienda o zona.

No uses un CSV histórico o una muestra de ejemplo como si fueran precios actuales.

## 1. Requisitos

- Python 3.11 o superior.
- Una base de datos PostgreSQL accesible desde el proceso del bot.
- Un bot creado con `@BotFather` en Telegram.
- Una URL HTTPS pública si se usa webhook (Render u otro hosting).

## 2. Instalación local en Windows

```powershell
cd C:\Programación\BotSupermercados
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edita `.env` y configura `TELEGRAM_TOKEN` y `DATABASE_URL`. Mantén el token fuera de Git.

Para desarrollo local puedes ejecutar:

```powershell
python main.py
```

Esto levanta Flask en el puerto 10000, pero Telegram no podrá enviar webhooks a `localhost` sin un túnel HTTPS. En producción, configura el webhook con la URL HTTPS de tu hosting.

## 3. Despliegue tipo Render

1. Sube esta carpeta a un repositorio privado de Git.
2. Crea un servicio Web Service desde el repositorio.
3. Añade las variables `TELEGRAM_TOKEN`, `DATABASE_URL` y `DB_SSLMODE=require`.
4. El comando de inicio es `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`.
5. Configura el webhook de Telegram a `https://TU-DOMINIO/TU_TELEGRAM_TOKEN`.

El código expone la ruta `/` como comprobación de salud y `/telegram-webhook` para recibir actualizaciones. El token ya no forma parte de la URL del webhook. Usa siempre HTTPS y no publiques el token.

Para registrar el webhook una vez desplegado, abre esta URL sustituyendo los valores (no la publiques ni compartas el token):

`https://api.telegram.org/bot<TU_TOKEN>/setWebhook?url=https://<TU-DOMINIO>/telegram-webhook`

## 4. Base de datos

Las tablas se crean automáticamente al iniciar `main.py`, con prefijo propio: `grocery_users`, `supermarkets`, `user_supermarkets`, `shopping_sessions` y `products`.

No se usa la tabla `users` del bot original para evitar conflictos.

## 5. Importar precios

El importador acepta un CSV con estas columnas obligatorias:

```csv
supermarket,name,price,captured_at
Mercadona,Leche semidesnatada 1 L,1.05,2026-10-24
```

Las columnas opcionales son `source,product_url,brand,package_size,unit,unit_price,postal_code,source_key,canonical_key,available,region_scope`. `price` es siempre el precio del envase. `unit_price` solo debe rellenarse si la fuente publica el precio por unidad de medida; no se calcula a partir de supuestos. `region_scope=national` solo debe usarse cuando la fuente confirme que el precio es nacional. Si el precio depende de tienda/zona, incluye `postal_code`. El ejemplo anterior solo explica el formato; **no es un precio de referencia real y no debe cargarse como dato de producción**.

Ejecuta:

```powershell
python import_prices.py ruta\al\fichero.csv --source "nombre-fuente"
```

El precio debe ser el precio de paquete. Para comparar por unidad de medida, incluye también `unit_price` publicado y `unit` (por ejemplo, `kg` o `l`). Para comparar productos equivalentes de distintas cadenas, incluye `canonical_key` con el mismo identificador solo cuando marca/formato/tamaño sean equivalentes. Si no se aporta, solo se comparan nombres normalizados idénticos; no se adivina que dos marcas blancas sean equivalentes. El matching textual de esta versión es deliberadamente básico: revisa el resultado antes de confiar en una equivalencia.

La comparación solo acepta registros con fecha de los últimos tres días. Ajusta este criterio conscientemente si la fuente publica con otra frecuencia; no elimines la fecha ni ocultes la antigüedad.


## 6. Conectores automáticos y límites

- **OpenCesta**: Mercadona, DIA y Ahorramás según las zonas que publique la release.
- **Alcampo y Carrefour**: lectura de datos estructurados públicos JSON-LD. Las URLs de búsqueda se pueden ajustar con `ALCAMPO_SEARCH_URL_TEMPLATE` y `CARREFOUR_SEARCH_URL_TEMPLATE` (admiten `{query}` y `{postal_code}`). La web puede exigir selección de tienda, cambiar su HTML o no exponer precio en JSON-LD; en esos casos el conector devuelve cero precios. El bot indica que la ubicación online no ha podido verificarse.
- **Lidl**: no hay catálogo online de alimentación completo en España. El bot no confunde el bazar online con el supermercado de alimentación. Para Lidl se utiliza el importador CSV con precios comprobados y fecha/fuente.

Los comercios indican que los precios y el surtido pueden variar por tienda o zona. Un precio de web no debe interpretarse como tarifa garantizada de una tienda física.

## 7. Variables de entorno adicionales

```env
PRICE_MAX_AGE_DAYS=3
# Opcionales; solo si cambian las rutas de búsqueda:
# ALCAMPO_SEARCH_URL_TEMPLATE=https://.../{query}
# CARREFOUR_SEARCH_URL_TEMPLATE=https://.../{query}
```

## 8. Comandos del bot

- `/start` o `/ayuda`: instrucciones.
- `/supermercados`: editar preferencias permanentes.
- `/cp 28001`: guardar código postal.
- `/comparar`: pedir la lista con pasos guiados.
- También puedes enviar la lista directamente como texto.

Las preferencias son persistentes. Los supermercados extra se guardan solo en la sesión temporal de la lista.

## 7. Siguientes mejoras recomendadas

1. Crear un adaptador de datos por supermercado con pruebas de contrato.
2. Cargar y comparar por código postal/zona.
3. Usar identificadores de producto y tamaño/unidad, no solo similitud textual.
4. Registrar origen, fecha de captura y disponibilidad para cada precio.
5. Mostrar enlaces al producto y avisar de precios sin cobertura.
6. Añadir pruebas automatizadas y rotación de logs.

## Atribución de datos

Si importas datos de CholloSúper, conserva la atribución exigida por su licencia: `Fuente: CholloSúper (chollosuper.com), consultado el [fecha]`. Comprueba siempre las condiciones de reutilización de cualquier otra fuente.


## 8. Conector automático OpenCesta

El conector está en `price_sources/api_connectors.py`. Descarga los assets Parquet de la última release de `ruvelro/opencesta`, los guarda en `/tmp/micestita_opencesta` y mantiene una caché en memoria durante 6 horas. El CP se resuelve con `opencesta zone <CP>`; no se convierte por prefijo en una zona inventada.

Variables opcionales:

```env
OPENCESTA_CACHE_TTL_SECONDS=21600
OPENCESTA_CACHE_DIR=/tmp/micestita_opencesta
PRICE_HTTP_TIMEOUT=30
```

La dependencia `opencesta` se instala desde su subdirectorio `core` mediante `requirements.txt` para disponer del comando `opencesta zone`. Si se despliega en un entorno que no permite instalar dependencias Git, el conector no podrá resolver zonas Mercadona y lo registrará en el log.

**Cobertura y precisión:** OpenCesta publica datos por cadena/zona y fecha. El código postal se utiliza para resolver la zona cuando el adaptador lo permite, pero el precio almacenado se etiqueta como regional (`postal_code = NULL`) porque no es una tarifa individual del domicilio. La fuente, fecha, zona y precio de referencia se conservan en `raw_data`. Las tiendas que no aparezcan en el snapshot no devolverán resultados.

**Importante:** después de actualizar este código, vuelve a configurar el webhook de Telegram con la ruta `/telegram-webhook`. Si el token anterior apareció en logs, revócalo en BotFather y configura el nuevo valor de `TELEGRAM_TOKEN`.
