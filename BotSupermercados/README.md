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
- Código postal opcional, respetado cuando el dato importado tiene zona.

## Limitación importante sobre los datos

El bot y la base de datos quedan funcionales, pero este repositorio **no trae un recolector automático de los cinco supermercados**. No se inventan endpoints ni precios. Antes de comparar en Telegram, hay que cargar datos reales mediante `import_prices.py` o desarrollar conectores mantenibles para fuentes concretas.

Fuentes públicas investigadas como posibles siguientes pasos:
- CholloSúper publica ficheros abiertos bajo CC BY 4.0, pero su descarga abierta incluye una cesta fija, índice, frescura y bajadas; no equivale a un catálogo completo para responder cualquier lista. Atribución requerida: `Fuente: CholloSúper (chollosuper.com), consultado el [fecha]`.
- OpenCesta publica un dataset histórico Parquet, pero su README indica cobertura de cadenas y zonas en desarrollo; no debe darse por hecho que hay catálogo actual completo de Mercadona, Carrefour, Lidl, DIA y Alcampo simultáneamente.
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

El código expone la ruta `/` como comprobación de salud y `/<TOKEN>` para recibir actualizaciones, siguiendo el patrón del bot original. Usa siempre HTTPS y no publiques el token.

Para registrar el webhook una vez desplegado, abre esta URL sustituyendo los valores (no la publiques ni compartas el token):

`https://api.telegram.org/bot<TU_TOKEN>/setWebhook?url=https://<TU-DOMINIO>/<TU_TOKEN>`

## 4. Base de datos

Las tablas se crean automáticamente al iniciar `main.py`, con prefijo propio: `grocery_users`, `supermarkets`, `user_supermarkets`, `shopping_sessions` y `products`.

No se usa la tabla `users` del bot original para evitar conflictos.

## 5. Importar precios

El importador acepta un CSV con estas columnas obligatorias:

```csv
supermarket,name,price,captured_at
Mercadona,Leche semidesnatada 1 L,1.05,2026-10-24
```

Las columnas opcionales son `source,product_url,brand,package_size,unit,postal_code,source_key,canonical_key,available`. El ejemplo anterior solo explica el formato; **no es un precio de referencia real y no debe cargarse como dato de producción**.

Ejecuta:

```powershell
python import_prices.py ruta\al\fichero.csv --source "nombre-fuente"
```

El precio debe ser el precio de paquete y la unidad debe ser explícita si se quiere comparar por unidad de medida. Para comparar productos equivalentes de distintas cadenas, incluye `canonical_key` con el mismo identificador solo cuando marca/formato/tamaño sean equivalentes. Si no se aporta, solo se comparan nombres normalizados idénticos; no se adivina que dos marcas blancas sean equivalentes. El matching textual de esta versión es deliberadamente básico: revisa el resultado antes de confiar en una equivalencia.

La comparación solo acepta registros con fecha de los últimos tres días. Ajusta este criterio conscientemente si la fuente publica con otra frecuencia; no elimines la fecha ni ocultes la antigüedad.

## 6. Comandos del bot

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
