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

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
SPREADSHEET_NAME = "Ebay_App"

CACHE_LOCK = threading.Lock()
EXECUTION_LOCK = threading.Lock()
SHEET_CLIENT = None
SPREADSHEET_OBJ = None

CACHE_LISTINGS_METADATA = {}
IDS_EXISTENTES_SUBASTAS = set()
EBAY_TOKEN_CACHE = {"access_token": None, "expires_at": 0.0}

http_session = requests.Session()

def obtener_cliente_sheets():
    global SHEET_CLIENT, SPREADSHEET_OBJ
    if SPREADSHEET_OBJ is not None: return SPREADSHEET_OBJ
    
    creds_info = {
        "type": "service_account",
        "project_id": os.environ.get("GOOGLE_PROJECT_ID"),
        "private_key": os.environ.get("GOOGLE_PRIVATE_KEY").replace("\\n", "\n"),
        "client_email": os.environ.get("GOOGLE_CLIENT_EMAIL"),
        "token_uri": "https://oauth2.googleapis.com/token"
    }
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    SHEET_CLIENT = gspread.authorize(creds)
    SPREADSHEET_OBJ = SHEET_CLIENT.open(SPREADSHEET_NAME)
    return SPREADSHEET_OBJ

def inicializar_cache_memoria():
    global CACHE_LISTINGS_METADATA, IDS_EXISTENTES_SUBASTAS
    try:
        sheet = obtener_cliente_sheets()
        
        # Listings Cache
        try:
            ws_listings = sheet.worksheet("Listings")
            filas_l = ws_listings.get_all_values()
            with CACHE_LOCK:
                CACHE_LISTINGS_METADATA.clear()
                if len(filas_l) > 1:
                    for idx, f in enumerate(filas_l[1:], start=2):
                        if f and f[0]:
                            item_id = str(f[0]).strip()
                            apariciones = int(f[3]) if len(f) > 3 and f[3].isdigit() else 1
                            CACHE_LISTINGS_METADATA[item_id] = {"row_index": idx, "no_apariciones": apariciones}
        except Exception as e:
            logging.error(f"Error cargando caché Listings: {e}")

        # Auctions Cache
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
            logging.error(f"Error cargando caché Auctions: {e}")

        logging.info(f"[Cache] Sincronizada: {len(CACHE_LISTINGS_METADATA)} listings y {len(IDS_EXISTENTES_SUBASTAS)} subastas.")
    except Exception as e:
        logging.error(f"[Error Cache] {e}")

def obtener_token_ebay():
    global EBAY_TOKEN_CACHE
    if EBAY_TOKEN_CACHE["access_token"] and time.time() < (EBAY_TOKEN_CACHE["expires_at"] - 300):
        return EBAY_TOKEN_CACHE["access_token"]

    encoded = base64.b64encode(f"{os.environ.get('EBAY_CLIENT_ID')}:{os.environ.get('EBAY_CLIENT_SECRET')}".encode()).decode()
    resp = http_session.post("https://api.ebay.com/identity/v1/oauth2/token", 
                             headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"},
                             data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
    if resp.status_code == 200:
        data = resp.json()
        EBAY_TOKEN_CACHE.update({"access_token": data["access_token"], "expires_at": time.time() + data["expires_in"]})
        return EBAY_TOKEN_CACHE["access_token"]
    raise Exception(f"Auth Error: {resp.text}")

def peticion_ebay_con_retry(url, headers):
    for i in range(4):
        try:
            resp = http_session.get(url, headers=headers, timeout=15)
            if resp.status_code == 200: return resp
            if resp.status_code == 429: time.sleep(2**i + random.uniform(1, 2))
        except: 
            time.sleep(2)
    return None

def extraer_info_vendedor_ubicacion(item):
    seller_info = item.get("seller", {})
    vendedor = seller_info.get("username", "Desconocido")
    item_location = item.get("itemLocation", {})
    location = f"{item_location.get('city', '')}, {item_location.get('country', '')}".strip(", ")
    return vendedor, (location if location else "Estados Unidos")

def buscar_ebay_recursivo_adaptativo(p_min, p_max, headers):
    items_acumulados = []
    url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD,buyingOptions:FIXED_PRICE&limit=100&sort=newlyListed"
    resp = peticion_ebay_con_retry(url, headers)
    if resp and resp.status_code == 200:
        items_acumulados.extend(resp.json().get("itemSummaries", []))
    return items_acumulados

def barrido_listings_incremental_worker():
    if not EXECUTION_LOCK.acquire(blocking=False):
        return
    try:
        logging.info("--- [INICIO] Barrido incremental de Buy It Now ---")
        sheet = obtener_cliente_sheets()
        token = obtener_token_ebay()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        
        ws_listings = sheet.worksheet("Listings")
        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_str = datetime.now(tz_cdmx).strftime("%Y-%m-%d %H:%M:%S")

        rangos_base = [("0", "150"), ("151", "400"), ("401", "800"), ("801", "1500"), ("1501", "3000"), ("3001", "999999")]
        items_encontrados_hoy = set()
        nuevos_listings = []
        actualizaciones_batch = []

        for p_min, p_max in rangos_base:
            items_rango = buscar_ebay_recursivo_adaptativo(p_min, p_max, headers)
            for item in items_rango:
                item_id = str(item.get("itemId", "")).strip()
                if not item_id: continue
                
                items_encontrados_hoy.add(item_id)
                title = item.get("title", "")
                item_url = item.get("itemWebUrl", "")
                price = float(item.get("price", {}).get("value", 0))
                vendedor, location = extraer_info_vendedor_ubicacion(item)

                with CACHE_LOCK:
                    meta = CACHE_LISTINGS_METADATA.get(item_id)

                if meta:
                    row_idx = meta["row_index"]
                    nuevo_apariciones = meta["no_apariciones"] + 1
                    meta["no_apariciones"] = nuevo_apariciones
                    actualizaciones_batch.append({'range': f'C{row_idx}:F{row_idx}', 'values': [[ahora_str, nuevo_apariciones, vendedor, location]]})
                    actualizaciones_batch.append({'range': f'K{row_idx}', 'values': [["Activo"]]})
                else:
                    nuevos_listings.append([
                        item_id, ahora_str, ahora_str, 1, vendedor, location,
                        "PSA 10", ahora_str, title, price, "Activo", "Buy It Now", price, 1, item_url
                    ])

        if nuevos_listings:
            ws_listings.append_rows(nuevos_listings, value_input_option='USER_ENTERED')
            filas_actuales = len(ws_listings.get_all_values())
            inicio_nuevos = filas_actuales - len(nuevos_listings) + 1
            with CACHE_LOCK:
                for idx, fila in enumerate(nuevos_listings):
                    CACHE_LISTINGS_METADATA[fila[0]] = {"row_index": inicio_nuevos + idx, "no_apariciones": 1}

        with CACHE_LOCK:
            for item_id, meta in CACHE_LISTINGS_METADATA.items():
                if item_id not in items_encontrados_hoy:
                    actualizaciones_batch.append({'range': f'K{meta["row_index"]}', 'values': [["Vendido"]]})

        if actualizaciones_batch:
            ws_listings.batch_update(actualizaciones_batch)

        logging.info("--- [FIN] Barrido de listings completado ---")
    except Exception as e:
        logging.error(f"[Error Listings] {e}")
    finally:
        EXECUTION_LOCK.release()

def proceso_fondo_matutino_worker():
    try:
        logging.info("--- [INICIO] Proceso Matutino / Freeze Diario ---")
        sheet = obtener_cliente_sheets()
        token = obtener_token_ebay()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        
        ws_auctions = sheet.worksheet("Auctions")
        tz_cdmx = timezone(timedelta(hours=-6))
        hoy_str = datetime.now(tz_cdmx).strftime("%Y-%m-%d")
        fecha_registro_actual = datetime.now(tz_cdmx).strftime("%Y-%m-%d %H:%M:%S")

        offset = 0
        auctions_lote = []
        while offset < 1000:
            url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=buyingOptions:AUCTION,priceCurrency:USD&limit=200&offset={offset}&sort=newlyListed"
            resp = peticion_ebay_con_retry(url, headers)
            if not resp or resp.status_code != 200: break
            
            items = resp.json().get("itemSummaries", [])
            if not items: break
            
            for item in items:
                end_time = item.get("itemEndDate", "")
                if end_time:
                    try:
                        dt_cdmx = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(tz_cdmx)
                        if dt_cdmx.strftime("%Y-%m-%d") == hoy_str:
                            item_id = str(item.get("itemId", "")).strip()
                            with CACHE_LOCK:
                                existe = item_id in IDS_EXISTENTES_SUBASTAS
                            
                            if not existe:
                                IDS_EXISTENTES_SUBASTAS.add(item_id)
                                price_obj = item.get("currentBidPrice") or item.get("price", {})
                                vendedor, location = extraer_info_vendedor_ubicacion(item)
                                
                                auctions_lote.append([
                                    item_id, vendedor, location, "PSA 10", fecha_registro_actual, 
                                    item.get("title"), float(price_obj.get("value", 0)), 
                                    0.0, 0.0, int(item.get("bidCount", 0)), 0, 0, 
                                    dt_cdmx.strftime("%Y-%m-%d %H:%M:%S"), "Activa", item.get("itemWebUrl")
                                ])
                    except Exception:
                        continue

            if len(items) < 200: break
            offset += 200

        if auctions_lote:
            ws_auctions.append_rows(auctions_lote, value_input_option='USER_ENTERED')
            logging.info(f"[Google Sheets] Registradas {len(auctions_lote)} subastas nuevas.")
        else:
            logging.info("[Info] No hay más subastas nuevas para hoy.")

        barrido_listings_incremental_worker()
        logging.info("--- [FIN] Proceso Matutino Finalizado ---")
    except Exception as e:
        logging.error(f"[Error Matutino] {e}")

@app.route("/ping")
def ping():
    return "Pong!", 200

@app.route("/")
def home():
    return "Bot operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    threading.Thread(target=proceso_fondo_matutino_worker).start()
    return jsonify({"status": "success", "message": "Proceso iniciado."})

if __name__ == "__main__":
    try:
        inicializar_cache_memoria()
    except Exception as e:
        logging.error(f"Error en caché de arranque: {e}")
    app.run(host="0.0.0.0", port=10000)
