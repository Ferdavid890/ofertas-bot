import os
import base64
import requests
import time
import random
import threading
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
import gspread
from google.oauth2.service_account import Credentials

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# Configuración
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
    sheet = obtener_cliente_sheets()
    
    # Cache Listings
    ws_listings = sheet.worksheet("Listings")
    for idx, f in enumerate(ws_listings.get_all_values()[1:], start=2):
        if f and f[0]: CACHE_LISTINGS_METADATA[str(f[0]).strip()] = {"row_index": idx, "no_apariciones": int(f[3]) if len(f) > 3 and f[3].isdigit() else 1}
    
    # Cache Auctions
    ws_auctions = sheet.worksheet("Auctions")
    IDS_EXISTENTES_SUBASTAS.update(str(f[0]).strip() for f in ws_auctions.get_all_values()[1:] if f and f[0])
    logging.info(f"[Cache] Sincronizada: {len(CACHE_LISTINGS_METADATA)} listings y {len(IDS_EXISTENTES_SUBASTAS)} subastas.")

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
            time.sleep(2**i)
        except: pass
    return None

def extraer_info_vendedor_ubicacion(item):
    s = item.get("seller", {})
    l = item.get("itemLocation", {})
    return s.get("username", "N/A"), f"{l.get('city', '')}, {l.get('country', '')}".strip(", ")

def barrido_listings_incremental_worker():
    if not EXECUTION_LOCK.acquire(blocking=False): return
    try:
        logging.info("--- [INICIO] Barrido Buy It Now ---")
        sheet = obtener_cliente_sheets()
        token = obtener_token_ebay()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        ws_listings = sheet.worksheet("Listings")
        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_str = datetime.now(tz_cdmx).strftime("%Y-%m-%d %H:%M:%S")

        nuevos = []
        # Rangos de precio para abarcar todo
        for p in [("0", "500"), ("501", "999999")]:
            url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p[0]}..{p[1]}],buyingOptions:FIXED_PRICE&limit=100"
            resp = peticion_ebay_con_retry(url, headers)
            if resp and resp.status_code == 200:
                for item in resp.json().get("itemSummaries", []):
                    item_id = str(item.get("itemId", "")).strip()
                    if item_id not in CACHE_LISTINGS_METADATA:
                        v, l = extraer_info_vendedor_ubicacion(item)
                        nuevos.append([item_id, ahora_str, ahora_str, 1, v, l, "PSA 10", ahora_str, item.get("title"), float(item.get("price", {}).get("value", 0)), "Activo", "Buy It Now", 0, 0, item.get("itemWebUrl")])
        
        if nuevos: 
            ws_listings.append_rows(nuevos, value_input_option='USER_ENTERED')
            for f in nuevos: CACHE_LISTINGS_METADATA[f[0]] = {"row_index": 0, "no_apariciones": 1}
        logging.info("--- [FIN] Barrido Listings ---")
    finally: EXECUTION_LOCK.release()

def proceso_fondo_matutino_worker():
    try:
        logging.info("--- [INICIO] Proceso Matutino ---")
        sheet = obtener_cliente_sheets()
        token = obtener_token_ebay()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        ws_auctions = sheet.worksheet("Auctions")
        tz_cdmx = timezone(timedelta(hours=-6))
        hoy_str = datetime.now(tz_cdmx).strftime("%Y-%m-%d")

        offset = 0
        lote = []
        # END TIME SOONEST es la clave para ver lo que ves en web
        while offset < 1000:
            url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=buyingOptions:AUCTION&limit=200&offset={offset}&sort=endTimeSoonest"
            resp = peticion_ebay_con_retry(url, headers)
            if not resp or resp.status_code != 200: break
            
            items = resp.json().get("itemSummaries", [])
            if not items: break
            
            terminar = False
            for item in items:
                dt_cdmx = datetime.fromisoformat(item.get("itemEndDate", "").replace("Z", "+00:00")).astimezone(tz_cdmx)
                if dt_cdmx.strftime("%Y-%m-%d") > hoy_str: 
                    terminar = True; break
                
                if dt_cdmx.strftime("%Y-%m-%d") == hoy_str:
                    item_id = str(item.get("itemId", "")).strip()
                    if item_id not in IDS_EXISTENTES_SUBASTAS:
                        IDS_EXISTENTES_SUBASTAS.add(item_id)
                        v, l = extraer_info_vendedor_ubicacion(item)
                        lote.append([item_id, v, l, "PSA 10", datetime.now(tz_cdmx).strftime("%Y-%m-%d %H:%M:%S"), item.get("title"), float(item.get("currentBidPrice", {}).get("value", 0)), 0, 0, int(item.get("bidCount", 0)), 0, 0, dt_cdmx.strftime("%Y-%m-%d %H:%M:%S"), "Activa", item.get("itemWebUrl")])
            
            if terminar or len(items) < 200: break
            offset += 200

        if lote: ws_auctions.append_rows(lote, value_input_option='USER_ENTERED')
        barrido_listings_incremental_worker()
        logging.info("--- [FIN] Proceso Matutino ---")
    except Exception as e: logging.error(f"[Error] {e}")

@app.route("/ejecutar-freeze-diario")
def ejecutar():
    threading.Thread(target=proceso_fondo_matutino_worker).start()
    return jsonify({"status": "iniciado"})

if __name__ == "__main__":
    inicializar_cache_memoria()
    app.run(host="0.0.0.0", port=10000)
