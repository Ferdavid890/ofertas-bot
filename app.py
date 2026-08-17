import os
import base64
import requests
import time
import random
import threading
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote
from math import ceil
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from gspread.exceptions import APIError, WorksheetNotFound

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("lorcana_bot")

TZ_CDMX = timezone(timedelta(hours=-6))

app = Flask(__name__)
TOKEN_LOCK = threading.Lock()
SHEETS_LOCK = threading.Lock()
SCHEDULED_IDS_LOCK = threading.Lock()
scheduled_ids = set()

# ------------------------------------------------------------------
# NOTA SOBRE PLAN FREE:
# Render Free duerme el proceso tras ~15 min sin trafico HTTP entrante,
# lo cual mataria el scheduler y perderiamos precision en el sniping.
# Solucion sin costo: configurar un ping externo GRATUITO (UptimeRobot,
# cron-job.org, etc.) que llame a GET /health cada 5 minutos. Mientras
# el ping sea mas frecuente que el timeout de 15 min, el proceso nunca
# se duerme y el scheduler interno corre 24/7 sin interrupciones.
#
# Como respaldo adicional (por si Render reinicia el contenedor por un
# deploy, crash, cold start, etc. y se pierde el estado en memoria), al
# arrancar el proceso se ejecuta recuperar_snipers_pendientes(): lee la
# hoja "Auctions", detecta subastas del dia que aun no cerraron y
# reprograma sus jobs de sniping desde el closing_time ya guardado.
# ------------------------------------------------------------------


# ======================================================
# EBAY AUTH
# ======================================================
class EbayAuthManager:
    LIMITE_DIARIO_SEGURO = 4000  # margen mas amplio bajo el limite real de 5000/dia de eBay
    CONTADOR_PATH = "/tmp/ebay_call_counter.json"  # persiste el conteo entre redeploys del mismo dia

    def __init__(self):
        self.token_cache = {"access_token": None, "expires_at": 0.0}
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5)
        self.session.mount("https://", adapter)
        self.call_count_lock = threading.Lock()
        self.call_count = self._cargar_contador()

    def _cargar_contador(self):
        try:
            import json
            with open(self.CONTADOR_PATH, "r") as f:
                data = json.load(f)
                hoy = datetime.now(TZ_CDMX).strftime("%Y-%m-%d")
                if data.get("fecha") == hoy:
                    log.info(f"[EbayAuth] Contador recuperado tras reinicio: {data.get('total', 0)} llamadas ya usadas hoy.")
                    return data
        except Exception:
            pass
        return {"fecha": None, "total": 0}

    def _guardar_contador(self):
        try:
            import json
            with open(self.CONTADOR_PATH, "w") as f:
                json.dump(self.call_count, f)
        except Exception:
            pass  # si falla el guardado en disco, seguimos operando solo con memoria

    def _registrar_llamada(self):
        with self.call_count_lock:
            hoy = datetime.now(TZ_CDMX).strftime("%Y-%m-%d")
            if self.call_count["fecha"] != hoy:
                self.call_count["fecha"] = hoy
                self.call_count["total"] = 0
            self.call_count["total"] += 1
            self._guardar_contador()
            return self.call_count["total"]

    def obtener_token(self):
        with TOKEN_LOCK:
            ahora = time.time()
            if self.token_cache["access_token"] and ahora < (self.token_cache["expires_at"] - 300):
                return self.token_cache["access_token"]

            client_id = os.environ.get("EBAY_CLIENT_ID")
            client_secret = os.environ.get("EBAY_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise Exception("Faltan credenciales de eBay.")

            credentials = f"{client_id}:{client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()

            url = "https://api.ebay.com/identity/v1/oauth2/token"
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {encoded}",
                "Connection": "close"
            }
            body = {
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope"
            }

            resp = self.session.post(url, headers=headers, data=body, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self.token_cache["access_token"] = data.get("access_token")
                self.token_cache["expires_at"] = ahora + data.get("expires_in", 7200)
                return self.token_cache["access_token"]
            raise Exception(f"Error autenticando eBay: {resp.text}")

    def headers(self):
        return {
            "Authorization": f"Bearer {self.obtener_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "Connection": "close"
        }

    def peticion_con_retry(self, url, max_intentos=3, timeout=(5, 15)):
        ultimo_status = None
        ultimo_texto = ""
        for intento in range(max_intentos):
            usadas_hoy = self._registrar_llamada()  # se cuenta CADA intento real, no solo la llamada logica
            if usadas_hoy > self.LIMITE_DIARIO_SEGURO:
                log.error(f"Limite diario seguro de eBay alcanzado ({usadas_hoy}/{self.LIMITE_DIARIO_SEGURO}). "
                          f"Saltando el resto de llamadas de hoy.")
                return None
            try:
                response = self.session.get(url, headers=self.headers(), timeout=timeout)
                ultimo_status = response.status_code
                ultimo_texto = response.text[:300]
                if response.status_code == 200:
                    return response
                elif response.status_code == 429:
                    cuerpo = response.text[:200]
                    if ("request limit" in cuerpo.lower() or "daily" in cuerpo.lower()) and usadas_hoy > 2000:
                        # Solo lo tratamos como limite DIARIO agotado si ya llevamos un
                        # volumen alto de llamadas hoy; si no, es mas probable que sea
                        # un limite de RAFAGA momentaneo (calls/segundo), no el total diario.
                        log.error(f"eBay 429 - LIMITE DIARIO AGOTADO para {url} (llamadas hoy: {usadas_hoy}): {cuerpo}")
                        return None
                    espera = (2 ** (intento + 2)) + random.uniform(2, 4)
                    log.warning(f"eBay 429 (probable rate limit de rafaga) para {url}, reintentando en {espera:.1f}s")
                    time.sleep(espera)
                elif response.status_code >= 500:
                    espera = (2 ** (intento + 1)) + 1
                    log.warning(f"eBay {response.status_code} (error de servidor) para {url}, reintentando en {espera:.1f}s")
                    time.sleep(espera)
                elif response.status_code == 401:
                    log.warning(f"eBay 401 (token invalido/expirado) para {url}")
                    with TOKEN_LOCK:
                        self.token_cache["access_token"] = None
                    time.sleep(1)
                else:
                    log.warning(f"eBay respondio {response.status_code} para {url}: {response.text[:200]}")
                    break
            except requests.exceptions.RequestException as e:
                ultimo_status = f"EXCEPTION:{type(e).__name__}"
                ultimo_texto = str(e)[:300]
                log.warning(f"Error de red en intento {intento+1}: {e}")
                time.sleep(2)
        log.warning(f"eBay: se agotaron los reintentos para {url} | ultimo_status={ultimo_status} | respuesta={ultimo_texto}")
        return None


ebay_auth = EbayAuthManager()


class EbayUserTokenManager:
    """
    Maneja el OAuth User Token (distinto del Application Token de ebay_auth).
    Necesario SOLO para llamadas a la Trading API (GetItem) que consultan el
    estado real de items ya terminados - la Browse API con Application Token no
    sirve para eso. Se arranca desde un refresh_token de larga duracion (~18
    meses, generado una vez a mano via el flujo OAuth de eBay) guardado en la
    variable de entorno EBAY_USER_REFRESH_TOKEN, y se refresca solo cada ~2h.
    """
    def __init__(self):
        self._token = None
        self._exp = 0
        self._lock = threading.Lock()

    def obtener_token(self):
        with self._lock:
            if self._token and time.time() < self._exp - 60:
                return self._token
            refresh_token = os.environ.get("EBAY_USER_REFRESH_TOKEN")
            if not refresh_token:
                log.warning("[EbayUserToken] Falta EBAY_USER_REFRESH_TOKEN en el entorno.")
                return None
            client_id = os.environ.get("EBAY_CLIENT_ID")
            client_secret = os.environ.get("EBAY_CLIENT_SECRET")
            basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            try:
                resp = requests.post(
                    "https://api.ebay.com/identity/v1/oauth2/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
                    data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                          "scope": "https://api.ebay.com/oauth/api_scope"},
                    timeout=15
                )
            except requests.exceptions.RequestException as e:
                log.warning(f"[EbayUserToken] Error de red refrescando: {e}")
                return None
            if resp.status_code != 200:
                log.error(f"[EbayUserToken] Error refrescando token: status={resp.status_code} | {resp.text[:200]}")
                return None
            data = resp.json()
            self._token = data["access_token"]
            self._exp = time.time() + data.get("expires_in", 7200)
            return self._token


ebay_user_auth = EbayUserTokenManager()


# ======================================================
# ANALISIS / VALIDACION
# ======================================================
class MarketAnalyzer:
    @staticmethod
    def validar_item_critico(item):
        tiene_precio = ("price" in item) or ("currentBidPrice" in item)
        return tiene_precio and all(c in item for c in ["itemId", "itemWebUrl"])

    @staticmethod
    def obtener_precio(item):
        # Subastas sin pujas aun no traen "price", solo "currentBidPrice" (o viceversa segun el endpoint)
        p = item.get("price") or item.get("currentBidPrice") or {}
        try:
            return float(p.get("value", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def es_carta_psa_10(titulo):
        t = titulo.upper()
        if any(bw in t for bw in ["PSA 9", "PSA9", "PSA 8", "PSA8", "RAW", "UNGRADED", "LOT", "BUNDLE", "SEALED"]):
            return False
        patterns = [r"\bPSA\s*10\b", r"\bPSA\s*GEM\s*MT\s*10\b", r"\bGEM\s*MT\s*10\b",
                    r"\bGEM\s*MINT\s*10\b", r"\bGEM\s*MT\b", r"\bGEM-MT\b"]
        return any(re.search(pat, t) for pat in patterns)

    @staticmethod
    def extraer_info_vendedor_ubicacion(item):
        seller = item.get("seller", {})
        vendedor = seller.get("username", "Desconocido")
        loc = item.get("itemLocation", {})
        location = f"{loc.get('city', '')}, {loc.get('country', 'US')}".strip(", ")
        return vendedor, (location if location else "Estados Unidos")


# ======================================================
# BUSQUEDA EBAY
# ======================================================
class EbaySearchEngine:
    RANGOS = [("0", "999999")]  # una sola pasada; la recursion divide sola si hay overflow
    LIMIT = 100
    MAX_ITEMS_POR_QUERY = 3000  # tope de seguridad para no colgar el proceso en plan free

    def buscar_recursivo(self, query, p_min, p_max, sort_order, buying_option, stats, category_param="", extra_filter="", max_items=None):
        items_acumulados = {}
        url = self._build_url(query, p_min, p_max, sort_order, buying_option, 0, category_param, extra_filter)
        resp = ebay_auth.peticion_con_retry(url)
        if not resp:
            log.warning(f"[EbaySearch] Sin respuesta valida para '{query}' [{p_min}-{p_max}] {buying_option}")
            return items_acumulados

        data = resp.json()
        total_reportado = data.get("total", 0)
        stats["total_reportado"] += total_reportado
        log.info(f"[EbaySearch] '{query}' [{p_min}-{p_max}] {buying_option}: status={resp.status_code} total={total_reportado}")
        if total_reportado == 0:
            log.info(f"[EbaySearch] Respuesta cruda (sin resultados): {resp.text[:500]}")

        # Sin particion recursiva por precio, sin filtros server-side especiales: solo
        # paginacion directa hasta un tope fijo. Barato y simple.
        tope = max_items if max_items is not None else self.MAX_ITEMS_POR_QUERY
        paginas = ceil(min(total_reportado, tope) / self.LIMIT) if total_reportado > 0 else 1
        raw_titles_muestra = []
        raw_total_crudo = 0
        for p in range(paginas):
            if p > 0 and p % 5 == 0:
                log.info(f"[EbaySearch] '{query}' {buying_option}: progreso pagina {p}/{paginas} "
                         f"({raw_total_crudo} crudos acumulados hasta ahora)...")
            offset = p * self.LIMIT
            if offset == 0:
                data_p = data
            else:
                paged_url = self._build_url(query, p_min, p_max, sort_order, buying_option, offset, category_param, extra_filter)
                resp_p = ebay_auth.peticion_con_retry(paged_url)
                if not resp_p:
                    log.warning(f"[EbaySearch] '{query}' {buying_option}: FALLO al pedir pagina {p+1}/{paginas} (offset={offset})")
                    continue
                data_p = resp_p.json()

            pagina_items = data_p.get("itemSummaries", [])
            raw_total_crudo += len(pagina_items)
            if p == 0:
                raw_titles_muestra = [it.get("title", "") for it in pagina_items[:5]]
            for it in pagina_items:
                titulo = it.get("title", "")
                if MarketAnalyzer.validar_item_critico(it) and MarketAnalyzer.es_carta_psa_10(titulo):
                    items_acumulados[it["itemId"]] = it
            time.sleep(random.uniform(0.15, 0.35))
        log.info(f"[EbaySearch] '{query}' {buying_option}: paginas_pedidas={paginas} | items_crudos_recibidos={raw_total_crudo} | "
                 f"validos_tras_filtro_titulo={len(items_acumulados)} | muestra_primeros_5_titulos={raw_titles_muestra}")
        return items_acumulados

    def _build_url(self, query, p_min, p_max, sort_order, buying_option, offset, category_param="", extra_filter=""):
        return (
            f"https://api.ebay.com/buy/browse/v1/item_summary/search?q={query}"
            f"{category_param}&filter=price:[{p_min}..{p_max}],priceCurrency:USD,"
            f"buyingOptions:{{{buying_option}}}{extra_filter}&sort={sort_order}&limit={self.LIMIT}&offset={offset}"
        )

    def ejecutar_motor_general(self, buying_option, sort_order, queries, category_params=None, extra_filter="", max_items=None):
        stats = {"total_reportado": 0}
        todos_items = {}
        categorias = category_params if category_params else [""]
        for q in queries:
            for cat in categorias:
                for r in self.RANGOS:
                    resultados = self.buscar_recursivo(q, r[0], r[1], sort_order, buying_option, stats, cat, extra_filter, max_items)
                    todos_items.update(resultados)
        return todos_items, stats


# Configuracion de busqueda SEPARADA para cada job, para que ajustar una no afecte
# accidentalmente a la otra:
QUERIES_LISTINGS = ['Lorcana "PSA 10"', 'Lorcana PSA10', 'Lorcana "Gem Mint 10"']
# 183454 = CCG Individual Cards (Toys&Hobbies>Collectible Card Games) - la que ya teniamos.
# 183050 = Non-Sport Trading Card Singles (Collectibles>Non-Sport Trading Cards) - AGREGADA
# 17-ago-2026: confirmado con 3 casos reales (items 298568492420, 398260946266,
# 377408977233 - vendedores PSA y Probstein Auctions) que listings legitimos de Lorcana
# caen en esta categoria y NO en 183454 - se estaban perdiendo por completo, sin importar
# que el titulo matcheara perfecto.
# OJO: eBay Browse API NO acepta varias category_ids separadas por coma en un solo filtro
# (error real: "The number of categories in the request has exceeded the limit. Please
# reduce the number of categories to 1 or less.") - hay que hacer una llamada POR
# categoria y combinar resultados (ejecutar_motor_general ya lo hace, dedup automatico
# por id_item). Esto duplica el numero de llamadas/paginas por query - ver nota de cuota
# mas abajo si el uso diario sube mucho.
CATEGORY_LISTINGS = ["&category_ids=183454", "&category_ids=183050"]

QUERIES_AUCTIONS = ['Lorcana "PSA 10"', 'Lorcana PSA10', 'Lorcana "Gem Mint 10"']  # 3 variantes: ya no es caro por query gracias al fix de itemEndDate
CATEGORY_AUCTIONS = ["&category_ids=183454", "&category_ids=183050"]


# ======================================================
# GOOGLE SHEETS (con reintentos y batching)
# ======================================================
def gspread_retry(fn, *args, max_intentos=4, **kwargs):
    for intento in range(max_intentos):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429 or (status and status >= 500):
                espera = (2 ** intento) + random.uniform(0.5, 1.5)
                log.warning(f"Sheets API {status}, reintentando en {espera:.1f}s...")
                time.sleep(espera)
                continue
            raise
        except requests.exceptions.RequestException as e:
            # Cubre SSLError, ConnectionError, Timeout, etc. (conexion corrupta/caida)
            espera = (2 ** intento) + random.uniform(0.5, 1.5)
            log.warning(f"Sheets: error de red ({type(e).__name__}), reintentando en {espera:.1f}s...")
            time.sleep(espera)
            continue
    raise Exception("Sheets API: se agotaron los reintentos.")


class SheetsManager:
    def __init__(self):
        self.spreadsheet = None
        self._client_lock = threading.Lock()

    def conectar(self):
        with self._client_lock:
            if self.spreadsheet:
                return self.spreadsheet
            creds = Credentials.from_service_account_info({
                "type": "service_account",
                "project_id": os.environ.get("GOOGLE_PROJECT_ID"),
                "private_key": os.environ.get("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n"),
                "client_email": os.environ.get("GOOGLE_CLIENT_EMAIL"),
                "token_uri": "https://oauth2.googleapis.com/token"
            }, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
            sheet_id = os.environ.get("GOOGLE_SHEET_ID")
            if not sheet_id:
                raise Exception(
                    "Falta la variable de entorno GOOGLE_SHEET_ID. Abrir por nombre "
                    "('Ebay_App') es ambiguo si existe mas de un archivo con ese nombre "
                    "accesible a la cuenta de servicio - hay que abrir por ID exacto."
                )
            self.spreadsheet = gspread.authorize(creds).open_by_key(sheet_id)
            try:
                self.spreadsheet.client.session.headers.update({"Connection": "close"})
            except Exception:
                pass  # si la version de gspread no expone .session, seguimos sin este ajuste
            return self.spreadsheet

    def worksheet(self, nombre):
        return gspread_retry(self.conectar().worksheet, nombre)

    def worksheet_or_create(self, nombre, headers):
        """Devuelve la pestaña; si no existe, la crea con encabezados."""
        try:
            return gspread_retry(self.conectar().worksheet, nombre)
        except WorksheetNotFound:
            ws = gspread_retry(self.conectar().add_worksheet, title=nombre, rows=1000, cols=len(headers))
            gspread_retry(ws.update, "A1", [headers])
            log.info(f"[Sheets] Pestaña '{nombre}' no existia, creada con encabezados.")
            return ws

    def get_all_records(self, ws, expected_headers=None):
        if expected_headers:
            return gspread_retry(ws.get_all_records, expected_headers=expected_headers)
        return gspread_retry(ws.get_all_records)

    def append_rows(self, ws, filas):
        if filas:
            return gspread_retry(ws.append_rows, filas, value_input_option="USER_ENTERED")

    def update_row_cells(self, ws, row_num, col_to_value: dict):
        data = [{"range": gspread.utils.rowcol_to_a1(row_num, col), "values": [[val]]}
                for col, val in col_to_value.items()]
        return gspread_retry(ws.batch_update, data)

    def batch_update_raw(self, ws, data):
        """data: lista de {'range': 'A1', 'values': [[valor]]} ya armada, para
        actualizar muchas celdas (de filas distintas) en una sola llamada."""
        if data:
            return gspread_retry(ws.batch_update, data)

sheets_manager = SheetsManager()
search_engine = EbaySearchEngine()

# Columnas hoja "Listings" (1-indexed):
# 1 id_item | 2 first_seen | 3 last_seen | 4 No_Apariciones | 5 Vendedor
# 6 Location | 7 no_psa | 8 date | 9 title_card | 10 price | 11 Status
# 12 listing_type | 13 fmv | 14 volume_7days | 15 Link
COL_LAST_SEEN, COL_NO_APARICIONES, COL_LISTING_STATUS, COL_PRICE = 3, 4, 11, 10
# Columna 16 va AL FINAL a proposito (mismo patron que en Auctions): confirma si un
# item marcado "Vendido" fue una venta real ("Sold") o solo se dio de baja sin
# vender ("Ended") - ver verificar_venta_real(). La llena un job aparte, por lotes,
# NO sync_listings directamente (evita bloquear el ciclo normal con miles de
# peticiones de golpe en el rollover de medianoche).
COL_VENTA_CONFIRMADA = 16
LISTINGS_HEADERS = ["id_item", "first_seen", "last_seen", "No_Apariciones", "Vendedor",
                     "Location", "no_psa", "date", "title_card", "price", "Status",
                     "listing_type", "fmv", "volume_7days", "Link", "venta_confirmada"]

PRICE_HISTORY_HEADERS = ["id_item", "timestamp", "date", "price_anterior", "price_nuevo", "cambio_usd", "cambio_pct", "title_card", "Link"]

# Columnas hoja "Auctions" (1-indexed):
# 1 id_item | 2 vendedor | 3 location | 4 grado | 5 date | 6 title
# 7 precio_inicial | 8 precio_t60 | 9 precio_final | 10 bids_iniciales
# 11 bids_t60 | 12 bids_final | 13 closing_time | 14 status | 15 url
COL_PRECIO_T60, COL_BIDS_T60 = 8, 11
COL_PRECIO_FINAL, COL_BIDS_FINAL = 9, 12
COL_STATUS = 14
COL_PRECIO_T1, COL_BIDS_T1 = 17, 18
# Columnas 16-18 van AL FINAL a proposito (despues de Link): asi los numeros de columna
# de arriba (COL_PRECIO_T60, COL_STATUS, etc.) nunca cambian sin importar si estas
# columnas extra existen o no.
# 16: version legible en CDMX de scheduled_closing_time (UTC).
# 17-18: checkpoint EXTRA a T-1s (ademas del T-2s ya existente en col 9/12) - da un
# segundo dato mas cerca del cierre por si la puja final sube mucho en el ultimo
# instante y T-2s se queda corto. El T-2s NO se quita: sirve de respaldo si el
# checkpoint de T-1s llega a fallar por falta de margen de red.
AUCTIONS_HEADERS = ["id_item", "Vendedor", "Location", "no_psa", "date", "title_card",
                     "initial_price", "final_price_60s", "final_price_2s", "bids",
                     "bids_60s", "bids_2s", "scheduled_closing_time", "status", "Link",
                     "scheduled_closing_time_cdmx", "final_price_1s", "bids_1s"]


# ======================================================
# LISTINGS (Buy It Now) - job cada hora
# ======================================================
def sync_listings():
    inicio = time.time()
    log.info("[Listings] Iniciando sincronizacion")
    try:
        items, stats = search_engine.ejecutar_motor_general("FIXED_PRICE", "newlyListed", queries=QUERIES_LISTINGS, category_params=CATEGORY_LISTINGS)
        ahora_str = datetime.now(TZ_CDMX).strftime("%Y-%m-%d %H:%M:%S")
        fecha_solo_dia = datetime.now(TZ_CDMX).strftime("%Y-%m-%d")

        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Listings")
            registros = sheets_manager.get_all_records(ws, expected_headers=LISTINGS_HEADERS)

        # Filas de HOY: id_item -> (row_num, No_Apariciones actual, price actual)
        filas_hoy = {}
        for i, r in enumerate(registros):
            if str(r.get("date", "")).startswith(fecha_solo_dia):
                item_id = str(r.get("id_item", ""))
                try:
                    apariciones = int(r.get("No_Apariciones", 1) or 1)
                except (TypeError, ValueError):
                    apariciones = 1
                try:
                    precio_actual = float(r.get("price", 0) or 0)
                except (TypeError, ValueError):
                    precio_actual = 0.0
                filas_hoy[item_id] = (i + 2, apariciones, precio_actual)

        filas_nuevas = []
        actualizaciones = []
        historial_precios = []
        encontrados_hoy = set()

        for item_id, it in items.items():
            encontrados_hoy.add(item_id)
            title = it.get("title", "")
            price_nuevo = MarketAnalyzer.obtener_precio(it)
            url = it.get("itemWebUrl", "")

            if item_id in filas_hoy:
                # Ya lo vimos hoy: actualizar last_seen, No_Apariciones y asegurar status Activo
                row_num, apariciones, precio_anterior = filas_hoy[item_id]
                actualizaciones.append({"range": gspread.utils.rowcol_to_a1(row_num, COL_LAST_SEEN), "values": [[ahora_str]]})
                actualizaciones.append({"range": gspread.utils.rowcol_to_a1(row_num, COL_NO_APARICIONES), "values": [[apariciones + 1]]})
                actualizaciones.append({"range": gspread.utils.rowcol_to_a1(row_num, COL_LISTING_STATUS), "values": [["Activo"]]})

                if abs(price_nuevo - precio_anterior) > 0.001:
                    # Cambio de precio: actualizar el precio en Listings Y dejar registro en PriceHistory
                    actualizaciones.append({"range": gspread.utils.rowcol_to_a1(row_num, COL_PRICE), "values": [[price_nuevo]]})
                    cambio_usd = round(price_nuevo - precio_anterior, 2)
                    cambio_pct = round(((price_nuevo - precio_anterior) / precio_anterior) * 100, 2) if precio_anterior else 0
                    historial_precios.append([item_id, ahora_str, fecha_solo_dia, precio_anterior, price_nuevo, cambio_usd, cambio_pct, title, url])
                continue

            vendedor, location = MarketAnalyzer.extraer_info_vendedor_ubicacion(it)
            filas_nuevas.append([
                item_id, ahora_str, ahora_str, 1, vendedor, location,
                "PSA 10", ahora_str, title, price_nuevo, "Activo", "Buy It Now", price_nuevo, 0, url
            ])

        # Los que tenian fila hoy pero ya NO aparecieron en esta busqueda: marcar Vendido
        for item_id, (row_num, _, _) in filas_hoy.items():
            if item_id not in encontrados_hoy:
                actualizaciones.append({"range": gspread.utils.rowcol_to_a1(row_num, COL_LISTING_STATUS), "values": [["Vendido"]]})

        if filas_nuevas:
            with SHEETS_LOCK:
                sheets_manager.append_rows(ws, filas_nuevas)
        if actualizaciones:
            with SHEETS_LOCK:
                sheets_manager.batch_update_raw(ws, actualizaciones)
        if historial_precios:
            with SHEETS_LOCK:
                ws_hist = sheets_manager.worksheet_or_create("PriceHistory", PRICE_HISTORY_HEADERS)
                sheets_manager.append_rows(ws_hist, historial_precios)

        vendidos = sum(1 for a in actualizaciones if a["values"][0][0] == "Vendido")
        log.info(f"[Listings] Nuevos: {len(filas_nuevas)} | Actualizados: {len(filas_hoy) - vendidos} | Marcados Vendido: {vendidos} | "
                 f"Cambios de precio: {len(historial_precios)} | eBay total_reportado: {stats['total_reportado']} | items validos tras filtro: {len(items)} | Duracion: {round(time.time()-inicio,1)}s")
    except Exception as e:
        log.error(f"[Listings] Error: {e}", exc_info=True)


def verificar_venta_real(item_id_completo):
    """
    Consulta GetItem de la Trading API de eBay (API oficial, requiere User Token -
    no el Application Token que usamos para el resto del bot) para saber si un
    item que ya no esta activo termino con una venta real ("Sold", QuantitySold>0)
    o se dio de baja sin vender ("Ended"). Reemplaza el intento anterior via
    scraping de la pagina publica, que eBay bloqueaba con 403 desde la IP del
    datacenter de Render.
    Devuelve "Sold", "Ended", "Desconocido" (respuesta valida pero sin dato claro
    de QuantitySold) o None (fallo la consulta, hay que reintentar despues).
    """
    try:
        token = ebay_user_auth.obtener_token()
        if not token:
            return None
        legacy_id = item_id_completo.split("|")[1] if "|" in item_id_completo else item_id_completo
        xml_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{legacy_id}</ItemID>'
            '<DetailLevel>ReturnAll</DetailLevel>'
            '</GetItemRequest>'
        )
        headers = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml"
        }
        resp = requests.post("https://api.ebay.com/ws/api.dll", headers=headers,
                              data=xml_body.encode("utf-8"), timeout=15)
        if resp.status_code != 200:
            log.warning(f"[VerificarVenta] GetItem status={resp.status_code} item={legacy_id}: {resp.text[:200]}")
            return None

        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        root = ET.fromstring(resp.text)
        ack = root.findtext("e:Ack", default="", namespaces=ns)
        if ack not in ("Success", "Warning"):
            errores = root.findtext("e:Errors/e:LongMessage", default="(sin detalle)", namespaces=ns)
            log.warning(f"[VerificarVenta] GetItem Ack={ack} item={legacy_id}: {errores}")
            return None

        qty_sold_txt = root.findtext("e:Item/e:SellingStatus/e:QuantitySold", default=None, namespaces=ns)
        if qty_sold_txt is None:
            return "Desconocido"
        try:
            return "Sold" if int(qty_sold_txt) > 0 else "Ended"
        except ValueError:
            return "Desconocido"
    except Exception as e:
        log.warning(f"[VerificarVenta] Error consultando GetItem para {item_id_completo}: {e}")
        return None


def verificar_ventas_pendientes():
    """
    Job APARTE de sync_listings (no lo bloquea): recorre items en "Vendido" que
    aun no tienen venta_confirmada y valida contra GetItem (Trading API oficial)
    si fue una venta real o solo se dio de baja. Procesa un LOTE acotado por
    corrida (no todo de golpe) para no tardarse una eternidad - en el rollover de
    medianoche pueden pasar a Vendido cientos/miles de items a la vez, y esto se
    va poniendo al dia en varias corridas sucesivas de este job (cada 20 min).
    """
    LOTE = 40
    log.info("[VerificarVenta] Buscando items Vendido sin confirmar...")
    try:
        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Listings")
            registros = sheets_manager.get_all_records(ws, expected_headers=LISTINGS_HEADERS)

        pendientes = [
            (i + 2, r) for i, r in enumerate(registros)
            if r.get("Status") == "Vendido" and not r.get("venta_confirmada")
        ][:LOTE]

        if not pendientes:
            log.info("[VerificarVenta] Nada pendiente por confirmar.")
            return

        actualizaciones = []
        for row_num, r in pendientes:
            item_id = r.get("id_item")
            if not item_id:
                continue
            resultado = verificar_venta_real(item_id)
            if resultado:
                actualizaciones.append({
                    "range": gspread.utils.rowcol_to_a1(row_num, COL_VENTA_CONFIRMADA),
                    "values": [[resultado]]
                })
            time.sleep(random.uniform(0.3, 0.6))  # pacing suave, es API oficial no scraping

        if actualizaciones:
            with SHEETS_LOCK:
                sheets_manager.batch_update_raw(ws, actualizaciones)

        log.info(f"[VerificarVenta] Revisados {len(pendientes)} | Confirmados {len(actualizaciones)} "
                 f"| el resto (si quedo) se procesa en la siguiente corrida")
    except Exception as e:
        log.error(f"[VerificarVenta] Error: {e}", exc_info=True)


# ======================================================
# AUCTIONS - deteccion + sniping preciso con APScheduler
# ======================================================
def snipe_job(item_id, row_num, fase):
    try:
        resp = ebay_auth.peticion_con_retry(f"https://api.ebay.com/buy/browse/v1/item/{quote(item_id, safe='')}")
        if not resp:
            log.warning(f"[Sniper {fase}] No se pudo obtener detalle de {item_id}")
            return
        detalle = resp.json()
        precio = MarketAnalyzer.obtener_precio(detalle)
        bids = detalle.get("bidCount", 0)

        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Auctions")
            if fase == "t60":
                sheets_manager.update_row_cells(ws, row_num, {
                    COL_PRECIO_T60: precio, COL_BIDS_T60: bids, COL_STATUS: "Monitoreado"
                })
                log.info(f"[Sniper T-60s] {item_id} -> ${precio} / {bids} bids")
            elif fase == "t2":
                sheets_manager.update_row_cells(ws, row_num, {
                    COL_PRECIO_FINAL: precio, COL_BIDS_FINAL: bids, COL_STATUS: "Finalizado"
                })
                log.info(f"[Sniper T-2s] {item_id} -> ${precio} / {bids} bids FINAL")
            else:  # t1 - checkpoint extra, mas cerca del cierre que T-2s
                sheets_manager.update_row_cells(ws, row_num, {
                    COL_PRECIO_T1: precio, COL_BIDS_T1: bids, COL_STATUS: "Finalizado"
                })
                log.info(f"[Sniper T-1s] {item_id} -> ${precio} / {bids} bids FINAL")
    except Exception as e:
        log.error(f"[Sniper {fase}] Error en {item_id}: {e}", exc_info=True)
    finally:
        with SCHEDULED_IDS_LOCK:
            scheduled_ids.discard(f"{item_id}:{fase}")


def programar_sniper(item_id, row_num, dt_cierre_utc):
    ahora_utc = datetime.now(timezone.utc)
    t60 = dt_cierre_utc - timedelta(seconds=60)
    t2 = dt_cierre_utc - timedelta(seconds=2)
    t1 = dt_cierre_utc - timedelta(seconds=1)

    with SCHEDULED_IDS_LOCK:
        if t60 > ahora_utc and f"{item_id}:t60" not in scheduled_ids:
            scheduler.add_job(snipe_job, trigger=DateTrigger(run_date=t60),
                               args=[item_id, row_num, "t60"],
                               id=f"snipe_t60_{item_id}", replace_existing=True,
                               misfire_grace_time=30)
            scheduled_ids.add(f"{item_id}:t60")

        if t2 > ahora_utc and f"{item_id}:t2" not in scheduled_ids:
            scheduler.add_job(snipe_job, trigger=DateTrigger(run_date=t2),
                               args=[item_id, row_num, "t2"],
                               id=f"snipe_t2_{item_id}", replace_existing=True,
                               misfire_grace_time=10)
            scheduled_ids.add(f"{item_id}:t2")

        if t1 > ahora_utc and f"{item_id}:t1" not in scheduled_ids:
            scheduler.add_job(snipe_job, trigger=DateTrigger(run_date=t1),
                               args=[item_id, row_num, "t1"],
                               id=f"snipe_t1_{item_id}", replace_existing=True,
                               misfire_grace_time=5)
            scheduled_ids.add(f"{item_id}:t1")


def sync_auctions():
    inicio = time.time()
    log.info("[Auctions] Buscando nuevas subastas del dia")
    try:
        # Tope fijo simple (2000 items = 20 paginas), igual que la version anterior que
        # si funcionaba. Si un item no trae itemEndDate en el resumen de busqueda, se
        # descarta directamente (sin pedir su detalle aparte) - eso es lo que mas cuota
        # nos estaba costando antes.
        items, stats = search_engine.ejecutar_motor_general("AUCTION", "endingSoonest", queries=QUERIES_AUCTIONS, category_params=CATEGORY_AUCTIONS, max_items=1000)
        hoy_str = datetime.now(TZ_CDMX).strftime("%Y-%m-%d")

        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Auctions")
            registros = sheets_manager.get_all_records(ws, expected_headers=AUCTIONS_HEADERS)

        ids_en_sheet = {str(r.get("id_item")): (i + 2, r) for i, r in enumerate(registros)}

        filas_nuevas = []
        pendientes_programar = []

        log.info(f"[Auctions] Procesando {len(items)} items encontrados (filtrando ya conocidos)...")
        ultima_posicion_hoy = -1
        contador_ya_conocidos = 0
        contador_finalizados = 0
        contador_detalle_fallo = 0
        contador_sin_fecha_nunca = 0
        contador_fecha_no_es_hoy = 0
        for idx, (item_id, it) in enumerate(items.items()):
            if idx > 0 and idx % 25 == 0:
                log.info(f"[Auctions] Progreso: {idx}/{len(items)} items procesados...")
            if item_id in ids_en_sheet:
                # Ya lo conocemos: NO volver a pedir detalle a eBay.
                # Reusamos el scheduled_closing_time que ya esta en la hoja.
                contador_ya_conocidos += 1
                row_num, row_data = ids_en_sheet[item_id]
                if row_data.get("status") == "Finalizado":
                    contador_finalizados += 1
                    continue
                closing_time_str = str(row_data.get("scheduled_closing_time", ""))
                if not closing_time_str:
                    continue
                try:
                    dt_cierre_utc = datetime.fromisoformat(closing_time_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                programar_sniper(item_id, row_num, dt_cierre_utc)
                continue

            # Item nuevo: itemEndDate practicamente NUNCA viene en el resumen de busqueda
            # (confirmado empiricamente: 0 de 1961 items lo traian). No hay atajo posible
            # aqui - si queremos saber la fecha real de cierre, hay que pedirla aparte.
            closing_time_str = it.get("itemEndDate", "")
            if not closing_time_str:
                time.sleep(random.uniform(0.3, 0.6))  # pacing: evitar rafaga cuando hay muchos items nuevos de golpe
                detalle_resp = ebay_auth.peticion_con_retry(
                    f"https://api.ebay.com/buy/browse/v1/item/{quote(item_id, safe='')}")
                if not detalle_resp:
                    contador_detalle_fallo += 1
                    continue
                detalle = detalle_resp.json()
                closing_time_str = detalle.get("itemEndDate", "")
                if not closing_time_str:
                    contador_sin_fecha_nunca += 1
                    continue

            dt_cierre_utc = datetime.fromisoformat(closing_time_str.replace("Z", "+00:00"))
            dt_cierre_cdmx = dt_cierre_utc.astimezone(TZ_CDMX)
            if dt_cierre_cdmx.strftime("%Y-%m-%d") != hoy_str:
                contador_fecha_no_es_hoy += 1
                if idx < 5 or idx % 100 == 0:
                    log.info(f"[Auctions] Ejemplo descartado: item {item_id} cierra {dt_cierre_cdmx.strftime('%Y-%m-%d %H:%M')} CDMX (hoy es {hoy_str})")
                continue

            ultima_posicion_hoy = idx

            title = it.get("title", "")
            precio_inicial = MarketAnalyzer.obtener_precio(it)
            url = it.get("itemWebUrl", "")
            vendedor, location = MarketAnalyzer.extraer_info_vendedor_ubicacion(it)
            bids_iniciales = it.get("bidCount", 0)

            filas_nuevas.append([
                item_id, vendedor, location, "PSA 10", hoy_str, title,
                precio_inicial, "", "", bids_iniciales, "", "", closing_time_str, "Activo", url,
                dt_cierre_cdmx.strftime("%Y-%m-%d %H:%M:%S")
            ])
            pendientes_programar.append((item_id, dt_cierre_utc))

        if filas_nuevas:
            with SHEETS_LOCK:
                start_row = len(registros) + 2
                sheets_manager.append_rows(ws, filas_nuevas)
            for offset, (item_id, dt_cierre_utc) in enumerate(pendientes_programar):
                programar_sniper(item_id, start_row + offset, dt_cierre_utc)

        duracion = round(time.time() - inicio, 1)
        margen_seguridad = f"{len(items) - ultima_posicion_hoy} posiciones de margen antes del tope" if ultima_posicion_hoy >= 0 else "N/A"
        log.info(f"[Auctions] DIAGNOSTICO -> ya_conocidos: {contador_ya_conocidos} | finalizados: {contador_finalizados} | "
                 f"detalle_fallo(429/error): {contador_detalle_fallo} | sin_fecha_nunca(ni con detalle): {contador_sin_fecha_nunca} | "
                 f"fecha_valida_pero_no_es_hoy: {contador_fecha_no_es_hoy}")
        log.info(f"[Auctions] Nuevas: {len(filas_nuevas)} | Snipers activos: {len(scheduled_ids)} | eBay total_reportado: {stats['total_reportado']} | "
                 f"items validos tras filtro: {len(items)} | ULTIMA subasta de hoy encontrada en posicion {ultima_posicion_hoy}/{len(items)} ({margen_seguridad}) | Duracion: {duracion}s")
    except Exception as e:
        log.error(f"[Auctions] Error: {e}", exc_info=True)


def recuperar_snipers_pendientes():
    """
    Se ejecuta UNA vez al arrancar el proceso. Si Render reinicio el
    contenedor (deploy, crash, cold start tras dormirse por falta de
    ping, etc.), esto reconstruye los sniper jobs a partir de lo que
    ya esta guardado en la hoja "Auctions", en vez de depender de que
    sync_auctions() los vuelva a "descubrir" por busqueda en eBay.
    """
    log.info("[Recuperacion] Revisando subastas activas del dia en Sheets...")
    try:
        hoy_str = datetime.now(TZ_CDMX).strftime("%Y-%m-%d")
        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Auctions")
            registros = sheets_manager.get_all_records(ws, expected_headers=AUCTIONS_HEADERS)

        recuperados = 0
        for i, row in enumerate(registros):
            row_num = i + 2
            item_id = str(row.get("id_item", ""))
            status = row.get("status", "")
            closing_time_str = str(row.get("scheduled_closing_time", ""))
            if not item_id or status == "Finalizado" or not closing_time_str:
                continue
            try:
                dt_cierre_utc = datetime.fromisoformat(closing_time_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt_cierre_utc.astimezone(TZ_CDMX).strftime("%Y-%m-%d") != hoy_str:
                continue
            if dt_cierre_utc <= datetime.now(timezone.utc):
                continue

            programar_sniper(item_id, row_num, dt_cierre_utc)
            recuperados += 1

        log.info(f"[Recuperacion] Snipers reprogramados: {recuperados}")
    except Exception as e:
        log.error(f"[Recuperacion] Error: {e}", exc_info=True)


def reset_diario():
    with SCHEDULED_IDS_LOCK:
        scheduled_ids.clear()
    log.info("[Reset] Nuevo dia iniciado, cache de snipers limpiado.")


def arranque_inicial():
    """
    Se ejecuta UNA sola vez, en un hilo aparte, apenas el proceso termina de
    levantar (deploy nuevo, redeploy, o cold start tras dormirse en el plan
    free). Corre listings y luego auctions del dia en curso, para que el
    sistema quede poblado sin depender de que alguien dispare los endpoints
    /ejecutar-listings y /ejecutar-subastas a mano.

    Va en hilo aparte para no bloquear el arranque de Flask/gunicorn (Render
    espera que el puerto abra rapido, y estas dos funciones pueden tardar
    varios segundos por la cuota de eBay).

    Es seguro correrlo mas de una vez el mismo dia (ej. si Render reinicia el
    contenedor a medias del dia): tanto sync_listings como sync_auctions
    dedupan contra lo que ya esta en el Sheet en vez de duplicar filas.
    """
    log.info("[Arranque] Primera pasada del dia: listings -> auctions...")
    try:
        sync_listings()
    except Exception as e:
        log.error(f"[Arranque] Error en sync_listings inicial: {e}", exc_info=True)
    try:
        sync_auctions()
    except Exception as e:
        log.error(f"[Arranque] Error en sync_auctions inicial: {e}", exc_info=True)
    log.info("[Arranque] Primera pasada completa. De aqui en adelante corre solo: "
             "listings cada 1h, auctions + reset todos los dias a las 00:01/00:05 CDMX.")


# ======================================================
# SCHEDULER
# ======================================================
scheduler = BackgroundScheduler(
    timezone=timezone.utc,
    executors={"default": APThreadPoolExecutor(10)},  # reducido para no saturar RAM/CPU del plan free
    job_defaults={"coalesce": True, "max_instances": 1}
)


def iniciar_scheduler():
    scheduler.add_job(reset_diario, CronTrigger(hour=6, minute=1, timezone=timezone.utc), id="reset_diario")
    # Auctions corre UNA sola vez al dia, poco despues del reset (00:05 CDMX): las
    # subastas duran minimo 1 dia completo, asi que es imposible que una subasta se
    # publique hoy y cierre hoy mismo. El conjunto de "lo que cierra hoy" queda fijo
    # desde este barrido - no hace falta repetirlo durante el dia. El monitoreo fino
    # (T-60s/T-2s) lo hace el sniper programado por separado, sin re-buscar nada.
    #
    # Los jobs programados (IntervalTrigger/CronTrigger) definen el RITMO regular
    # a partir de ahora (listings cada 1h desde este momento, auctions/reset cada
    # dia a las 00:01/00:05 CDMX). La PRIMERA pasada del dia en curso la dispara
    # arranque_inicial() por separado, mas abajo, para no depender de esperar a
    # la proxima hora en punto o al proximo 00:05 CDMX.
    scheduler.add_job(sync_listings, IntervalTrigger(hours=1), id="sync_listings")
    scheduler.add_job(sync_auctions, CronTrigger(hour=6, minute=5, timezone=timezone.utc), id="sync_auctions")
    # verificar_ventas_pendientes: version scraping (bloqueada por eBay con 403 desde IP
    # de datacenter, confirmado 16-ago-2026) fue reemplazada por GetItem de la Trading
    # API con User Token (ver EbayUserTokenManager) - API oficial, sin bloqueo.
    scheduler.add_job(verificar_ventas_pendientes, IntervalTrigger(minutes=20), id="verificar_ventas_pendientes")
    scheduler.start()
    recuperar_snipers_pendientes()
    threading.Thread(target=arranque_inicial, daemon=True).start()
    log.info("Scheduler iniciado: listings cada 1h, auctions detectadas 1 vez/dia (00:05 CDMX), "
             "snipers puntuales T-60s/T-2s/T-1s, verificacion de ventas via GetItem cada 20min.")


# ======================================================
# FLASK: health-check (usado por el ping externo de keep-alive) + triggers manuales
# ======================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "scheduler_running": scheduler.running,
        "jobs_activos": len(scheduler.get_jobs()),
        "snipers_pendientes": len(scheduled_ids),
        "ebay_llamadas_hoy": ebay_auth.call_count.get("total", 0),
        "ebay_limite_seguro": EbayAuthManager.LIMITE_DIARIO_SEGURO
    })


@app.route("/ebay-quota", methods=["GET"])
def ebay_quota():
    """Consulta el uso REAL de cuota diaria directamente contra eBay (Developer Analytics API),
    en vez de depender de nuestro contador interno (que se resetea si el proceso reinicia)."""
    try:
        token = ebay_auth.obtener_token()
        resp = requests.get(
            "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/?api_context=buy&api_name=browse",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        return jsonify({"status_code": resp.status_code, "data": resp.json()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/ejecutar-listings", methods=["GET"])
def trigger_listings_manual():
    threading.Thread(target=sync_listings, daemon=True).start()
    return jsonify({"status": "disparado"})


@app.route("/ejecutar-subastas", methods=["GET"])
def trigger_auctions_manual():
    threading.Thread(target=sync_auctions, daemon=True).start()
    return jsonify({"status": "disparado"})


@app.route("/ejecutar-verificar-ventas", methods=["GET"])
def trigger_verificar_ventas_manual():
    threading.Thread(target=verificar_ventas_pendientes, daemon=True).start()
    return jsonify({"status": "disparado"})


iniciar_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
