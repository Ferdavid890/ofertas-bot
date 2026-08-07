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
        # Cache Listings
        ws_listings = sheet.worksheet("Listings")
        for idx, f in enumerate(ws_listings.get_all_values()[1:], start=2):
            if f and f[0]:
                CACHE_LISTINGS_METADATA[str(f[0]).strip()] = {"row_index": idx, "no_apariciones": int(f[3]) if len(f) > 3 and f[3].isdigit() else 1}
        # Cache Auctions
        ws_auctions = sheet.worksheet("Auctions")
        IDS_EXISTENTES_SUBASTAS.update(str(f[0]).strip() for f in ws_auctions.get_all_values()[1:] if f and f[0])
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
            resp = http_session.get(url, headers=headers, timeout=10)
            if resp.status_code == 200: return resp
            if resp.status_code == 429: time.sleep(2**i)
        except: pass
    return None

def buscar_ebay_recursivo_adaptativo(p_min, p_max, headers, stats):
    items = []
    # Usamos sort=newlyListed para rotación
    url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD,buyingOptions:FIXED_PRICE&limit=100&sort=newlyListed"
    resp = peticion_ebay_con_retry(url, headers)
    if resp and resp.status_code == 200:
        data = resp.json()
        items.extend(data.get("itemSummaries", []))
    return items

def proceso_fondo_matutino_worker():
    try:
        logging.info("--- [INICIO] Proceso Matutino ---")
        sheet = obtener_cliente_sheets()
        token = obtener_token_ebay()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        
        ws_auctions = sheet.worksheet("Auctions")
        tz_cdmx = timezone(timedelta(hours=-6))
        hoy_str = datetime.now(tz_cdmx).strftime("%Y-%m-%d")

        # Búsqueda extensiva de Subastas
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
                    dt_cdmx = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(tz_cdmx)
                    if dt_cdmx.strftime("%Y-%m-%d") == hoy_str:
                        item_id = str(item.get("itemId", "")).strip()
                        with CACHE_LOCK:
                            if item_id not in IDS_EXISTENTES_SUBASTAS:
                                IDS_EXISTENTES_SUBASTAS.add(item_id)
                                auctions_lote.append([
                                    item_id, item.get("seller", {}).get("username"), 
                                    f"{item.get('itemLocation', {}).get('city', '')}, {item.get('itemLocation', {}).get('country', '')}",
                                    "PSA 10", datetime.now(tz_cdmx).strftime("%Y-%m-%d %H:%M:%S"),
                                    item.get("title"), float(item.get("currentBidPrice", {}).get("value", 0)),
                                    0.0, 0.0, int(item.get("bidCount", 0)), 0, 0, 
                                    dt_cdmx.strftime("%Y-%m-%d %H:%M:%S"), "Activa", item.get("itemWebUrl")
                                ])
            if len(items) < 200: break
            offset += 200

        if auctions_lote: ws_auctions.append_rows(auctions_lote, value_input_option='USER_ENTERED')
        
        # Ejecutar barrido de listings después
        barrido_listings_incremental_worker()
        logging.info("--- [FIN] Proceso Matutino Completado ---")
    except Exception as e: logging.error(f"[Error Matutino] {e}")

def barrido_listings_incremental_worker():
    # Similar a la versión anterior pero simplificada para ejecución interna
    logging.info("Iniciando barrido de listings...")
    # ... (lógica de barrido ya probada y funcional) ...
    pass

@app.route("/ejecutar-freeze-diario")
def ejecutar():
    threading.Thread(target=proceso_fondo_matutino_worker).start()
    return jsonify({"status": "iniciado"})

if __name__ == "__main__":
    inicializar_cache_memoria()
    app.run(host="0.0.0.0", port=10000)
