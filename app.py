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
    try:
        ws = sheet.worksheet("Listings")
        filas = ws.get_all_values()
        with CACHE_LOCK:
            CACHE_LISTINGS_METADATA.clear()
            for idx, f in enumerate(filas[1:], start=2):
                if f and f[0]: CACHE_LISTINGS_METADATA[str(f[0]).strip()] = {"row_index": idx, "no_apariciones": int(f[3]) if len(f) > 3 and f[3].isdigit() else 1}
    except: pass
    # Cache Auctions
    try:
        ws = sheet.worksheet("Auctions")
        filas = ws.get_all_values()
        with CACHE_LOCK:
            IDS_EXISTENTES_SUBASTAS = {str(f[0]).strip() for f in filas[1:] if f and f[0]}
    except: pass
    logging.info(f"[Cache] Sincronizada: {len(CACHE_LISTINGS_METADATA)} listings, {len(IDS_EXISTENTES_SUBASTAS)} auctions.")

def obtener_token_ebay():
    global EBAY_TOKEN_CACHE
    if EBAY_TOKEN_CACHE["access_token"] and time.time() < (EBAY_TOKEN_CACHE["expires_at"] - 300):
        return EBAY_TOKEN_CACHE["access_token"]
    
    creds = base64.b64encode(f"{os.environ.get('EBAY_CLIENT_ID')}:{os.environ.get('EBAY_CLIENT_SECRET')}".encode()).decode()
    resp = http_session.post("https://api.ebay.com/identity/v1/oauth2/token", 
                             headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
                             data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
    data = resp.json()
    EBAY_TOKEN_CACHE.update({"access_token": data["access_token"], "expires_at": time.time() + data["expires_in"]})
    return EBAY_TOKEN_CACHE["access_token"]

def peticion_ebay_con_retry(url, headers):
    for i in range(3):
        try:
            r = http_session.get(url, headers=headers, timeout=10)
            if r.status_code == 200: return r
            time.sleep(1)
        except: continue
    return None

def buscar_auctions_hoy(headers, stats):
    items_acumulados = []
    limit = 100
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=buyingOptions:AUCTION,endTime:[{hoy}T00:00:00Z..{hoy}T23:59:59Z]&limit={limit}&offset="
    
    resp = peticion_ebay_con_retry(url + "0", headers)
    if not resp or resp.status_code != 200: return []
    
    data = resp.json()
    total = data.get("total", 0)
    items_acumulados.extend(data.get("itemSummaries", []))
    
    logging.info(f"[Debug Auctions] Total encontrado en API: {total}")
    
    for page in range(1, ceil(total / limit)):
        r = peticion_ebay_con_retry(url + str(page * limit), headers)
        if r and r.status_code == 200: items_acumulados.extend(r.json().get("itemSummaries", []))
    
    return items_acumulados

def buscar_ebay_recursivo_adaptativo(p_min, p_max, headers, stats, buying_option="FIXED_PRICE"):
    items = []
    url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],buyingOptions:{buying_option}&limit=100&offset=0"
    resp = peticion_ebay_con_retry(url, headers)
    if not resp or resp.status_code != 200: return []
    
    data = resp.json()
    total = data.get("total", 0)
    
    if total > 2000 and (float(p_max) - float(p_min)) > 1:
        mid = round((float(p_min) + float(p_max)) / 2, 2)
        return buscar_ebay_recursivo_adaptativo(p_min, str(mid), headers, stats, buying_option) + \
               buscar_ebay_recursivo_adaptativo(str(mid + 0.01), p_max, headers, stats, buying_option)
    
    items.extend(data.get("itemSummaries", []))
    for page in range(1, ceil(total / 100)):
        r = peticion_ebay_con_retry(url.replace("offset=0", f"offset={page*100}"), headers)
        if r and r.status_code == 200: items.extend(r.json().get("itemSummaries", []))
    return items

def barrido_listings_incremental_worker():
    if not EXECUTION_LOCK.acquire(blocking=False): return
    try:
        token = obtener_token_ebay()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        ws = obtener_cliente_sheets().worksheet("Listings")
        stats = {"descargados": 0}
        
        rangos = [("0","150"), ("151","400"), ("401","800"), ("801","1500"), ("1501","9999")]
        encontrados_hoy = set()
        nuevos = []
        
        for min_p, max_p in rangos:
            items = buscar_ebay_recursivo_adaptativo(min_p, max_p, headers, stats, "FIXED_PRICE")
            for item in items:
                i_id = str(item.get("itemId", "")).strip()
                encontrados_hoy.add(i_id)
                if i_id not in CACHE_LISTINGS_METADATA:
                    nuevos.append([i_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "", 1, "", "", "PSA 10", "", item.get("title"), item.get("price",{}).get("value"), "Activo", "Buy It Now", item.get("price",{}).get("value"), 1, item.get("itemWebUrl")])
        
        if nuevos: ws.append_rows(nuevos)
        logging.info(f"[Listings] Barrido finalizado. Nuevos: {len(nuevos)}")
    finally: EXECUTION_LOCK.release()

def proceso_fondo_matutino_worker():
    try:
        headers = {"Authorization": f"Bearer {obtener_token_ebay()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        ws_auc = obtener_cliente_sheets().worksheet("Auctions")
        items = buscar_auctions_hoy(headers, {})
        
        nuevos_auc = []
        for item in items:
            i_id = str(item.get("itemId", "")).strip()
            if i_id not in IDS_EXISTENTES_SUBASTAS:
                IDS_EXISTENTES_SUBASTAS.add(i_id)
                nuevos_auc.append([
                    i_id, item.get("seller",{}).get("username"), "", "PSA 10", 
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), item.get("title"),
                    item.get("currentBidPrice", item.get("price",{})).get("value"), 0, 0, 
                    item.get("bidCount", 0), 0, 0, item.get("itemEndDate"), "Activa", item.get("itemWebUrl")
                ])
        
        if nuevos_auc: ws_auc.append_rows(nuevos_auc)
        barrido_listings_incremental_worker()
    except Exception as e: logging.error(f"Error matutino: {e}")

@app.route("/")
def home(): return "Bot Activo"

@app.route("/ejecutar-freeze-diario")
def trigger(): 
    threading.Thread(target=proceso_fondo_matutino_worker).start()
    return "Proceso iniciado"

if __name__ == "__main__":
    inicializar_cache_memoria()
    app.run(host="0.0.0.0", port=10000)
