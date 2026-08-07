import os
import base64
import requests
import time
import random
import threading
import gc
import logging
from math import ceil
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
import gspread
from google.oauth2.service_account import Credentials

# Configuración de Logging Profesional
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = "Ebay_App"

# Estructuras en Memoria y Locks de Concurrencia
CACHE_LOCK = threading.Lock()
EXECUTION_LOCK = threading.Lock()
SHEET_CLIENT = None
SPREADSHEET_OBJ = None

# Caché en memoria para metadatos de Listings y Subastas
CACHE_LISTINGS_METADATA = {}
IDS_EXISTENTES_SUBASTAS = set()

EBAY_TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0.0
}

# Sesión HTTP reutilizable con Keep-Alive
http_session = requests.Session()

def obtener_cliente_sheets():
    global SHEET_CLIENT, SPREADSHEET_OBJ
    if SPREADSHEET_OBJ is not None:
        return SPREADSHEET_OBJ
    
    client_email = os.environ.get("GOOGLE_CLIENT_EMAIL")
    private_key = os.environ.get("GOOGLE_PRIVATE_KEY")
    project_id = os.environ.get("GOOGLE_PROJECT_ID")

    if not client_email or not private_key or not project_id:
        raise Exception("Faltan variables de entorno de Google Sheets.")

    private_key = private_key.replace("\\n", "\n")
    creds_info = {
        "type": "service_account",
        "project_id": project_id,
        "private_key": private_key,
        "client_email": client_email,
        "token_uri": "https://oauth2.googleapis.com/token"
    }

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    SHEET_CLIENT = gspread.authorize(creds)
    SPREADSHEET_OBJ = SHEET_CLIENT.open(SPREADSHEET_NAME)
    return SPREADSHEET_OBJ

def inicializar_cache_memoria():
    """Carga inicial de metadatos de Listings y Subastas en memoria al arrancar."""
    global CACHE_LISTINGS_METADATA, IDS_EXISTENTES_SUBASTAS
    try:
        sheet = obtener_cliente_sheets()
        
        # Listings
        try:
            ws_listings = sheet.worksheet("Listings")
            filas_l = ws_listings.get_all_values()
            with CACHE_LOCK:
                CACHE_LISTINGS_METADATA.clear()
                if len(filas_l) > 1:
                    for idx, f in enumerate(filas_l[1:], start=2):
                        if f and f[0]:
                            item_id = str(f[0]).strip()
                            first_seen = f[1] if len(f) > 1 and f[1] else ""
                            try:
                                apariciones = int(f[3]) if len(f) > 3 and f[3].isdigit() else 1
                            except ValueError:
                                apariciones = 1
                            
                            CACHE_LISTINGS_METADATA[item_id] = {
                                "row_index": idx,
                                "first_seen": first_seen,
                                "no_apariciones": apariciones
                            }
        except Exception as e:
            logging.error(f"Error cargando caché de Listings: {str(e)}")

        # Subastas
        try:
            ws_auctions = sheet.worksheet("Auctions")
            filas_a = ws_auctions.get_all_values()
            with CACHE_LOCK:
                IDS_EXISTENTES_SUBASTAS.clear()
                if len(filas_a) > 1:
                    for f in filas_a[1:]:
                        if f and f[0]:
                            IDS_EXISTENTES_SUBASTAS.add(str(f[0]).strip())
        except Exception as e:
            logging.error(f"Error cargando caché de Subastas: {str(e)}")

        logging.info(f"[Cache] Sincronizada: {len(CACHE_LISTINGS_METADATA)} listings y {len(IDS_EXISTENTES_SUBASTAS)} subastas cargadas.")
    except Exception as e:
        logging.error(f"[Error Cache] No se pudo inicializar la caché: {str(e)}")

def obtener_token_ebay():
    global EBAY_TOKEN_CACHE
    ahora = time.time()
    
    if EBAY_TOKEN_CACHE["access_token"] and ahora < (EBAY_TOKEN_CACHE["expires_at"] - 300):
        return EBAY_TOKEN_CACHE["access_token"]

    client_id = os.environ.get("EBAY_CLIENT_ID")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise Exception("Faltan las credenciales de eBay.")

    credentials = f"{client_id}:{client_secret}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_credentials}"
    }
    body = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }

    response = http_session.post(url, headers=headers, data=body, timeout=10)
    if response.status_code == 200:
        data = response.json()
        EBAY_TOKEN_CACHE["access_token"] = data.get("access_token")
        expires_in = data.get("expires_in", 7200)
        EBAY_TOKEN_CACHE["expires_at"] = ahora + expires_in
        logging.info("[Token Manager] Token de eBay renovado exitosamente.")
        return EBAY_TOKEN_CACHE["access_token"]
    else:
        raise Exception(f"Error autenticando eBay: {response.text}")

def peticion_ebay_con_retry(url, headers):
    intentos = 4
    for intento in range(intentos):
        try:
            response = http_session.get(url, headers=headers, timeout=(5, 15))
            if response.status_code == 200:
                time.sleep(random.uniform(0.3, 0.7))
                return response
            elif response.status_code == 429:
                tiempo_espera = (2 ** intento) + random.uniform(1, 3)
                logging.warning(f"[Rate Limiter] Alerta 429. Reintentando en {tiempo_espera:.2f}s...")
                time.sleep(tiempo_espera)
            elif response.status_code >= 500:
                tiempo_espera = (2 ** intento) + 1
                logging.warning(f"[Error Servidor eBay {response.status_code}]. Reintentando en {tiempo_espera}s...")
                time.sleep(tiempo_espera)
            else:
                logging.warning(f"[eBay API Error] Código {response.status_code} para URL: {url} | Respuesta: {response.text}")
                break
        except requests.exceptions.RequestException as e:
            logging.error(f"[Excepción HTTP] {str(e)}. Reintentando...")
            time.sleep(2)
    return None

def extraer_info_vendedor_ubicacion(item):
    seller_info = item.get("seller", {})
    vendedor = seller_info.get("username", "Desconocido")
    
    item_location = item.get("itemLocation", {})
    country = item_location.get("country", "")
    city = item_location.get("city", "")
    location = f"{city}, {country}".strip(", ")
    if not location:
        location = "Estados Unidos"
    return vendedor, location

def buscar_ebay_recursivo_adaptativo(p_min, p_max, headers, stats, buying_option="FIXED_PRICE"):
    """Rangos Adaptativos calculando páginas reales basadas en el campo total para Listings o Subastas."""
    items_acumulados = []
    limit = 100
    
    search_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD,buyingOptions:{buying_option}&limit={limit}&offset=0"
    stats["consultas_ebay"] += 1
    resp = peticion_ebay_con_retry(search_url, headers)
    
    if not resp or resp.status_code != 200:
        return items_acumulados
        
    data = resp.json()
    total_resultados = data.get("total", 0)
    
    if total_resultados > 2000 and (float(p_max) - float(p_min)) > 1:
        punto_medio = round((float(p_min) + float(p_max)) / 2, 2)
        logging.info(f"[Rangos Adaptativos {buying_option}] [{p_min} - {p_max}] tiene {total_resultados} ítems. Dividiendo en: [{p_min} - {punto_medio}] y [{punto_medio + 0.01} - {p_max}]")
        items_acumulados.extend(buscar_ebay_recursivo_adaptativo(p_min, str(punto_medio), headers, stats, buying_option))
        items_acumulados.extend(buscar_ebay_recursivo_adaptativo(str(punto_medio + 0.01), p_max, headers, stats, buying_option))
        return items_acumulados

    items_acumulados.extend(data.get("itemSummaries", []))
    paginas_totales = ceil(total_resultados / limit) if total_resultados > 0 else 1
    
    for page in range(1, paginas_totales):
        offset = page * limit
        if offset >= 2000:
            break
        paged_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD,buyingOptions:{buying_option}&limit={limit}&offset={offset}"
        stats["consultas_ebay"] += 1
        resp = peticion_ebay_con_retry(paged_url, headers)
        if not resp or resp.status_code != 200:
            break
        items = resp.json().get("itemSummaries", [])
        if not items:
            break
        items_acumulados.extend(items)

    return items_acumulados

def barrido_listings_incremental_worker():
    if not EXECUTION_LOCK.acquire(blocking=False):
        logging.warning("[Lock Global] Barrido de listings en ejecución omitido por concurrencia.")
        return

    tiempo_inicio = time.time()
    stats = {"consultas_ebay": 0, "descargados": 0, "nuevos_agregados": 0, "actualizados": 0}

    try:
        logging.info("--- [INICIO] Barrido incremental de Buy It Now con seguimiento de estados ---")
        sheet = obtener_cliente_sheets()
        token = obtener_token_ebay()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        
        ws_listings = sheet.worksheet("Listings")
        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_str = datetime.now(tz_cdmx).strftime("%Y-%m-%d %H:%M:%S")

        if len(ws_listings.get_all_values()) == 0:
            ws_listings.update("A1:O1", [[
                "id_item", "first_seen", "last_seen", "No_Apariciones", "Vendedor", 
                "Location", "no_psa", "date", "title_card", "price", "Status", 
                "listing_type", "fmv", "volume_7days", "Link"
            ]])

        rangos_base = [
            ("0", "150"), ("151", "400"), ("401", "800"), 
            ("801", "1500"), ("1501", "3000"), ("3001", "999999")
        ]

        items_encontrados_hoy = set()
        nuevos_listings = []
        actualizaciones_batch = []

        for p_min, p_max in rangos_base:
            items_rango = buscar_ebay_recursivo_adaptativo(p_min, p_max, headers, stats, buying_option="FIXED_PRICE")
            stats["descargados"] += len(items_rango)

            for item in items_rango:
                item_id = str(item.get("itemId", "")).strip()
                if not item_id:
                    continue
                
                items_encontrados_hoy.add(item_id)
                title = item.get("title", "")
                item_url = item.get("itemWebUrl", "")
                price_info = item.get("price", {})
                price = float(price_info.get("value", 0)) if price_info.get("value") else 0.0
                vendedor, location = extraer_info_vendedor_ubicacion(item)

                with CACHE_LOCK:
                    meta = CACHE_LISTINGS_METADATA.get(item_id)

                if meta:
                    row_idx = meta["row_index"]
                    nuevo_apariciones = meta["no_apariciones"] + 1
                    meta["no_apariciones"] = nuevo_apariciones

                    actualizaciones_batch.append({'range': f'C{row_idx}:F{row_idx}', 'values': [[ahora_str, nuevo_apariciones, vendedor, location]]})
                    actualizaciones_batch.append({'range': f'K{row_idx}', 'values': [["Activo"]]})
                    stats["actualizados"] += 1
                else:
                    first_seen = ahora_str
                    last_seen = ahora_str
                    no_apariciones = 1
                    no_psa = "PSA 10"
                    date_val = ahora_str
                    listing_type = "Buy It Now"
                    fmv = price
                    volume_7days = 1
                    status = "Activo"

                    nuevos_listings.append([
                        item_id, first_seen, last_seen, no_apariciones, vendedor, location,
                        no_psa, date_val, title, price, status, listing_type, fmv, volume_7days, item_url
                    ])

            gc.collect()

        if nuevos_listings:
            ws_listings.append_rows(nuevos_listings, value_input_option='USER_ENTERED')
            filas_actuales = len(ws_listings.get_all_values())
            inicio_nuevos = filas_actuales - len(nuevos_listings) + 1
            with CACHE_LOCK:
                for idx, fila in enumerate(nuevos_listings):
                    item_id = fila[0]
                    CACHE_LISTINGS_METADATA[item_id] = {
                        "row_index": inicio_nuevos + idx,
                        "first_seen": fila[1],
                        "no_apariciones": 1
                    }
            stats["nuevos_agregados"] = len(nuevos_listings)

        with CACHE_LOCK:
            for item_id, meta in CACHE_LISTINGS_METADATA.items():
                if item_id not in items_encontrados_hoy:
                    row_idx = meta["row_index"]
                    actualizaciones_batch.append({'range': f'K{row_idx}', 'values': [["Vendido"]]})

        if actualizaciones_batch:
            ws_listings.batch_update(actualizaciones_batch)

        tiempo_total = time.time() - tiempo_inicio
        logging.info(f"--- [FIN] Barrido completado en {tiempo_total:.2f}s --- | Métricas: {stats}")

    except Exception as e:
        logging.error(f"[Error Crítico en Barrido Listings] {str(e)}")
    finally:
        EXECUTION_LOCK.release()

def revisar_y_actualizar_subastas_worker():
    try:
        sheet = obtener_cliente_sheets()
        ws_auctions = sheet.worksheet("Auctions")
        registros = ws_auctions.get_all_values()
        
        if len(registros) <= 1:
            return

        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        token = obtener_token_ebay()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }

        filas = registros[1:]
        actualizaciones_batch = []
        
        for idx, fila in enumerate(filas):
            if len(fila) < 14:
                continue
                
            item_id = fila[0]
            cierre_str = fila[12]
            status = fila[13]

            if status == "Finalizado":
                continue

            try:
                dt_cierre = datetime.strptime(cierre_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_cdmx)
                segundos_restantes = (dt_cierre - ahora_cdmx).total_seconds()
                fila_excel = idx + 2

                if status == "Activa" and -10 <= segundos_restantes <= 75:
                    item_url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
                    resp = peticion_ebay_con_retry(item_url, headers)
                    if resp and resp.status_code == 200:
                        data = resp.json()
                        precio = float(data.get("currentBidPrice", data.get("price", {})).get("value", 0))
                        bids = int(data.get("bidCount", 0))
                        actualizaciones_batch.append({'range': f'H{fila_excel}', 'values': [[precio]]})
                        actualizaciones_batch.append({'range': f'K{fila_excel}', 'values': [[bids]]})
                        actualizaciones_batch.append({'range': f'N{fila_excel}', 'values': [["Monitoreado 60s"]]})

                elif status == "Monitoreado 60s" and -15 <= segundos_restantes <= 10:
                    item_url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
                    resp = peticion_ebay_con_retry(item_url, headers)
                    if resp and resp.status_code == 200:
                        data = resp.json()
                        precio = float(data.get("currentBidPrice", data.get("price", {})).get("value", 0))
                        bids = int(data.get("bidCount", 0))
                        actualizaciones_batch.append({'range': f'I{fila_excel}', 'values': [[precio]]})
                        actualizaciones_batch.append({'range': f'L{fila_excel}', 'values': [[bids]]})
                        actualizaciones_batch.append({'range': f'N{fila_excel}', 'values': [["Finalizado"]]})

            except Exception as inner_e:
                logging.error(f"[Error Subasta {item_id}] {str(inner_e)}")

        if actualizaciones_batch:
            ws_auctions.batch_update(actualizaciones_batch)
            logging.info(f"[Batch Sheets] Actualizadas {len(actualizaciones_batch)} celdas de subastas.")

    except Exception as e:
        logging.error(f"[Error en revisión de subastas] {str(e)}")

def proceso_fondo_matutino_worker():
    try:
        logging.info("--- [INICIO] Proceso Matutino / Freeze Diario de Subastas y Listings ---")
        sheet = obtener_cliente_sheets()
        token = obtener_token_ebay()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        
        ws_auctions = sheet.worksheet("Auctions")
        if len(ws_auctions.get_all_values()) == 0:
            ws_auctions.update("A1:O1", [[
                "id_item", "Vendedor", "Location", "no_psa", "date", "title_card", 
                "initial_price", "final_price_60s", "final_price_2s", "bids", 
                "bids_60s", "bids_2s", "scheduled_closing_time", "status", "Link"
            ]])

        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        hoy_cdmx_str = ahora_cdmx.strftime("%Y-%m-%d")
        fecha_registro_actual = ahora_cdmx.strftime("%Y-%m-%d %H:%M:%S")

        rangos_base = [
            ("0", "150"), ("151", "400"), ("401", "800"), 
            ("801", "1500"), ("1501", "3000"), ("3001", "999999")
        ]

        auctions_lote = []
        stats_auc = {"consultas_ebay": 0}

        # Aplicamos la búsqueda adaptativa recursiva para subastas asegurando cobertura total
        for p_min, p_max in rangos_base:
            items_rango = buscar_ebay_recursivo_adaptativo(p_min, p_max, headers, stats_auc, buying_option="AUCTION")
            
            for item in items_rango:
                item_end_time = item.get("itemEndDate", "")
                if item_end_time:
                    try:
                        dt_utc = datetime.fromisoformat(item_end_time.replace("Z", "+00:00"))
                        dt_cdmx = dt_utc.astimezone(tz_cdmx)
                        if dt_cdmx.strftime("%Y-%m-%d") == hoy_cdmx_str:
                            item_id = str(item.get("itemId", "")).strip()
                            if item_id:
                                with CACHE_LOCK:
                                    existe = item_id in IDS_EXISTENTES_SUBASTAS
                                
                                if not existe:
                                    IDS_EXISTENTES_SUBASTAS.add(item_id)
                                    title = item.get("title", "")
                                    item_url = item.get("itemWebUrl", "")
                                    current_bid = float(item.get("currentBidPrice", item.get("price", {})).get("value", 0))
                                    bids_count = int(item.get("bidCount", 0))
                                    cierre_str = dt_cdmx.strftime("%Y-%m-%d %H:%M:%S")
                                    vendedor, location = extraer_info_vendedor_ubicacion(item)

                                    auctions_lote.append([
                                        item_id, vendedor, location, "PSA 10", fecha_registro_actual, title, 
                                        current_bid, 0.0, 0.0, bids_count, 0, 0, cierre_str, "Activa", item_url
                                    ])
                    except Exception:
                        continue

        if auctions_lote:
            ws_auctions.append_rows(auctions_lote, value_input_option='USER_ENTERED')
            logging.info(f"[Google Sheets] Registradas {len(auctions_lote)} subastas nuevas para hoy.")

        # Ejecutar en automático también el barrido de listings durante el proceso matutino
        barrido_listings_incremental_worker()

        logging.info("--- [FIN] Proceso Matutino Finalizado ---")
    except Exception as e:
        logging.error(f"[Error Proceso Matutino] {str(e)}")

with app.app_context():
    try:
        inicializar_cache_memoria()
    except Exception as e:
        logging.error(f"Error inicializando caché global en arranque: {str(e)}")

@app.route("/ping")
def ping():
    return "Pong! Servidor activo y corregido.", 200

@app.route("/")
def home():
    return "Bot de eBay operando correctamente con Listings y Auctions sincronizados 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    hilo = threading.Thread(target=proceso_fondo_matutino_worker)
    hilo.start()
    return jsonify({"status": "success", "message": "Proceso matutino de freeze y listings iniciado."})

@app.route("/actualizar-listings-nuevos", methods=["GET"])
def actualizar_listings_nuevos():
    hilo = threading.Thread(target=barrido_listings_incremental_worker)
    hilo.start()
    return jsonify({"status": "success", "message": "Escaneo incremental de Listings iniciado en segundo plano."})

@app.route("/verificar-subastas", methods=["GET"])
def verificar_subastas():
    hilo = threading.Thread(target=revisar_y_actualizar_subastas_worker)
    hilo.start()
    return jsonify({"status": "success", "message": "Verificación de subastas con Batch Update en curso."})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
