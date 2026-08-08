import os
import base64
import requests
import time
import random
import threading
import logging
import re
from math import ceil
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from gspread.exceptions import APIError

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
    def __init__(self):
        self.token_cache = {"access_token": None, "expires_at": 0.0}
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5)
        self.session.mount("https://", adapter)

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
        for intento in range(max_intentos):
            try:
                response = self.session.get(url, headers=self.headers(), timeout=timeout)
                if response.status_code == 200:
                    return response
                elif response.status_code == 429:
                    time.sleep((2 ** (intento + 1)) + random.uniform(1, 2))
                elif response.status_code >= 500:
                    time.sleep((2 ** (intento + 1)) + 1)
                elif response.status_code == 401:
                    with TOKEN_LOCK:
                        self.token_cache["access_token"] = None
                    time.sleep(1)
                else:
                    log.warning(f"eBay respondio {response.status_code} para {url}: {response.text[:200]}")
                    break
            except requests.exceptions.RequestException as e:
                log.warning(f"Error de red en intento {intento+1}: {e}")
                time.sleep(2)
        return None


ebay_auth = EbayAuthManager()


# ======================================================
# ANALISIS / VALIDACION
# ======================================================
class MarketAnalyzer:
    @staticmethod
    def validar_item_critico(item):
        return all(c in item for c in ["itemId", "price", "itemWebUrl"])

    @staticmethod
    def es_carta_psa_10(titulo):
        t = titulo.upper()
        if any(bw in t for bw in ["PSA 9", "PSA9", "PSA 8", "PSA8", "RAW", "UNGRADED", "LOT", "BUNDLE", "PACK", "BOX"]):
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
    QUERIES = ["Lorcana PSA 10", "Disney Lorcana PSA 10", "Disney Lorcana Enchanted PSA 10", "Lorcana Gem Mint 10"]
    RANGOS = [("0", "999999")]  # una sola pasada; la recursion divide sola si hay overflow
    CATEGORY_PARAM = "&category_ids=183454"
    LIMIT = 100
    MAX_ITEMS_POR_QUERY = 3000  # tope de seguridad para no colgar el proceso en plan free

    def buscar_recursivo(self, query, p_min, p_max, sort_order, buying_option, stats):
        items_acumulados = {}
        url = self._build_url(query, p_min, p_max, sort_order, buying_option, 0)
        resp = ebay_auth.peticion_con_retry(url)
        if not resp:
            return items_acumulados

        data = resp.json()
        total_reportado = data.get("total", 0)
        stats["total_reportado"] += total_reportado

        if total_reportado > self.MAX_ITEMS_POR_QUERY and (float(p_max) - float(p_min)) > 1:
            mid = round((float(p_min) + float(p_max)) / 2, 2)
            items_acumulados.update(self.buscar_recursivo(query, p_min, str(mid), sort_order, buying_option, stats))
            items_acumulados.update(self.buscar_recursivo(query, str(mid + 0.01), p_max, sort_order, buying_option, stats))
            return items_acumulados

        paginas = ceil(min(total_reportado, self.MAX_ITEMS_POR_QUERY) / self.LIMIT) if total_reportado > 0 else 1
        for p in range(paginas):
            offset = p * self.LIMIT
            if offset == 0:
                data_p = data
            else:
                paged_url = self._build_url(query, p_min, p_max, sort_order, buying_option, offset)
                resp_p = ebay_auth.peticion_con_retry(paged_url)
                if not resp_p:
                    continue
                data_p = resp_p.json()

            for it in data_p.get("itemSummaries", []):
                if MarketAnalyzer.validar_item_critico(it) and MarketAnalyzer.es_carta_psa_10(it.get("title", "")):
                    items_acumulados[it["itemId"]] = it
            time.sleep(random.uniform(0.15, 0.35))
        return items_acumulados

    def _build_url(self, query, p_min, p_max, sort_order, buying_option, offset):
        return (
            f"https://api.ebay.com/buy/browse/v1/item_summary/search?q={query}"
            f"{self.CATEGORY_PARAM}&filter=price:[{p_min}..{p_max}],priceCurrency:USD,"
            f"buyingOptions:{buying_option}&sort={sort_order}&limit={self.LIMIT}&offset={offset}"
        )

    def ejecutar_motor_general(self, buying_option, sort_order):
        stats = {"total_reportado": 0}
        todos_items = {}
        for q in self.QUERIES:
            for r in self.RANGOS:
                resultados = self.buscar_recursivo(q, r[0], r[1], sort_order, buying_option, stats)
                todos_items.update(resultados)
        return todos_items, stats


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
            self.spreadsheet = gspread.authorize(creds).open("Ebay_App")
            try:
                self.spreadsheet.client.session.headers.update({"Connection": "close"})
            except Exception:
                pass  # si la version de gspread no expone .session, seguimos sin este ajuste
            return self.spreadsheet

    def worksheet(self, nombre):
        return gspread_retry(self.conectar().worksheet, nombre)

    def get_all_records(self, ws):
        return gspread_retry(ws.get_all_records)

    def append_rows(self, ws, filas):
        if filas:
            return gspread_retry(ws.append_rows, filas, value_input_option="USER_ENTERED")

    def update_row_cells(self, ws, row_num, col_to_value: dict):
        data = [{"range": gspread.utils.rowcol_to_a1(row_num, col), "values": [[val]]}
                for col, val in col_to_value.items()]
        return gspread_retry(ws.batch_update, data)


sheets_manager = SheetsManager()
search_engine = EbaySearchEngine()

# Columnas hoja "Auctions" (1-indexed):
# 1 id_item | 2 vendedor | 3 location | 4 grado | 5 date | 6 title
# 7 precio_inicial | 8 precio_t60 | 9 precio_final | 10 bids_iniciales
# 11 bids_t60 | 12 bids_final | 13 closing_time | 14 status | 15 url
COL_PRECIO_T60, COL_BIDS_T60 = 8, 11
COL_PRECIO_FINAL, COL_BIDS_FINAL = 9, 12
COL_STATUS = 14


# ======================================================
# LISTINGS (Buy It Now) - job cada hora
# ======================================================
def sync_listings():
    inicio = time.time()
    log.info("[Listings] Iniciando sincronizacion")
    try:
        items, stats = search_engine.ejecutar_motor_general("FIXED_PRICE", "newlyListed")
        ahora_str = datetime.now(TZ_CDMX).strftime("%Y-%m-%d %H:%M:%S")
        fecha_solo_dia = datetime.now(TZ_CDMX).strftime("%Y-%m-%d")

        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Listings")
            registros = sheets_manager.get_all_records(ws)

        ids_existentes_hoy = {
            str(r.get("id_item", "")) for r in registros
            if str(r.get("date", "")).startswith(fecha_solo_dia)
        }

        filas_nuevas = []
        for item_id, it in items.items():
            if item_id in ids_existentes_hoy:
                continue
            title = it.get("title", "")
            price = float(it.get("price", {}).get("value", 0))
            url = it.get("itemWebUrl", "")
            vendedor, location = MarketAnalyzer.extraer_info_vendedor_ubicacion(it)
            filas_nuevas.append([
                item_id, ahora_str, ahora_str, 1, vendedor, location,
                "PSA 10", ahora_str, title, price, "Activo", "Buy It Now", price, 0, url
            ])

        if filas_nuevas:
            with SHEETS_LOCK:
                sheets_manager.append_rows(ws, filas_nuevas)

        log.info(f"[Listings] Nuevos agregados: {len(filas_nuevas)} | eBay total_reportado: {stats['total_reportado']} | items validos tras filtro: {len(items)} | Duracion: {round(time.time()-inicio,1)}s")
    except Exception as e:
        log.error(f"[Listings] Error: {e}", exc_info=True)


# ======================================================
# AUCTIONS - deteccion + sniping preciso con APScheduler
# ======================================================
def snipe_job(item_id, row_num, fase):
    try:
        resp = ebay_auth.peticion_con_retry(f"https://api.ebay.com/buy/browse/v1/item_summary/{item_id}")
        if not resp:
            log.warning(f"[Sniper {fase}] No se pudo obtener detalle de {item_id}")
            return
        detalle = resp.json()
        precio = float(detalle.get("price", {}).get("value", 0))
        bids = detalle.get("bidCount", 0)

        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Auctions")
            if fase == "t60":
                sheets_manager.update_row_cells(ws, row_num, {
                    COL_PRECIO_T60: precio, COL_BIDS_T60: bids, COL_STATUS: "Monitoreado"
                })
                log.info(f"[Sniper T-60s] {item_id} -> ${precio} / {bids} bids")
            else:
                sheets_manager.update_row_cells(ws, row_num, {
                    COL_PRECIO_FINAL: precio, COL_BIDS_FINAL: bids, COL_STATUS: "Finalizado"
                })
                log.info(f"[Sniper T-2s] {item_id} -> ${precio} / {bids} bids FINAL")
    except Exception as e:
        log.error(f"[Sniper {fase}] Error en {item_id}: {e}", exc_info=True)
    finally:
        with SCHEDULED_IDS_LOCK:
            scheduled_ids.discard(f"{item_id}:{fase}")


def programar_sniper(item_id, row_num, dt_cierre_utc):
    ahora_utc = datetime.now(timezone.utc)
    t60 = dt_cierre_utc - timedelta(seconds=60)
    t2 = dt_cierre_utc - timedelta(seconds=2)

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


def sync_auctions():
    inicio = time.time()
    log.info("[Auctions] Buscando nuevas subastas del dia")
    try:
        items, stats = search_engine.ejecutar_motor_general("AUCTION", "endingSoonest")
        hoy_str = datetime.now(TZ_CDMX).strftime("%Y-%m-%d")

        with SHEETS_LOCK:
            ws = sheets_manager.worksheet("Auctions")
            registros = sheets_manager.get_all_records(ws)

        ids_en_sheet = {str(r.get("id_item")): (i + 2, r) for i, r in enumerate(registros)}

        filas_nuevas = []
        pendientes_programar = []

        for item_id, it in items.items():
            if item_id in ids_en_sheet:
                # Ya lo conocemos: NO volver a pedir detalle a eBay.
                # Reusamos el scheduled_closing_time que ya esta en la hoja.
                row_num, row_data = ids_en_sheet[item_id]
                if row_data.get("status") == "Finalizado":
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

            # Item nuevo: aqui SI necesitamos el detalle para conocer itemEndDate
            detalle_resp = ebay_auth.peticion_con_retry(
                f"https://api.ebay.com/buy/browse/v1/item_summary/{item_id}")
            if not detalle_resp:
                continue
            detalle = detalle_resp.json()
            closing_time_str = detalle.get("itemEndDate", "")
            if not closing_time_str:
                continue

            dt_cierre_utc = datetime.fromisoformat(closing_time_str.replace("Z", "+00:00"))
            dt_cierre_cdmx = dt_cierre_utc.astimezone(TZ_CDMX)
            if dt_cierre_cdmx.strftime("%Y-%m-%d") != hoy_str:
                continue

            title = it.get("title", "")
            precio_inicial = float(it.get("price", {}).get("value", 0))
            url = it.get("itemWebUrl", "")
            vendedor, location = MarketAnalyzer.extraer_info_vendedor_ubicacion(it)
            bids_iniciales = it.get("bidCount", 0)

            filas_nuevas.append([
                item_id, vendedor, location, "PSA 10", hoy_str, title,
                precio_inicial, "", "", bids_iniciales, "", "", closing_time_str, "Activo", url
            ])
            pendientes_programar.append((item_id, dt_cierre_utc))

        if filas_nuevas:
            with SHEETS_LOCK:
                start_row = len(registros) + 2
                sheets_manager.append_rows(ws, filas_nuevas)
            for offset, (item_id, dt_cierre_utc) in enumerate(pendientes_programar):
                programar_sniper(item_id, start_row + offset, dt_cierre_utc)

        duracion = round(time.time() - inicio, 1)
        log.info(f"[Auctions] Nuevas: {len(filas_nuevas)} | Snipers activos: {len(scheduled_ids)} | eBay total_reportado: {stats['total_reportado']} | items validos tras filtro: {len(items)} | Duracion: {duracion}s")
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
            registros = sheets_manager.get_all_records(ws)

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
    scheduler.add_job(sync_listings, IntervalTrigger(hours=1), id="sync_listings", next_run_time=datetime.now())
    scheduler.add_job(sync_auctions, IntervalTrigger(minutes=5), id="sync_auctions", next_run_time=datetime.now())
    scheduler.start()
    recuperar_snipers_pendientes()
    log.info("Scheduler iniciado: listings cada 1h, auctions detectadas cada 5min, snipers puntuales T-60s/T-2s.")


# ======================================================
# FLASK: health-check (usado por el ping externo de keep-alive) + triggers manuales
# ======================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "scheduler_running": scheduler.running,
        "jobs_activos": len(scheduler.get_jobs()),
        "snipers_pendientes": len(scheduled_ids)
    })


@app.route("/ejecutar-listings", methods=["GET"])
def trigger_listings_manual():
    threading.Thread(target=sync_listings, daemon=True).start()
    return jsonify({"status": "disparado"})


@app.route("/ejecutar-subastas", methods=["GET"])
def trigger_auctions_manual():
    threading.Thread(target=sync_auctions, daemon=True).start()
    return jsonify({"status": "disparado"})


iniciar_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
