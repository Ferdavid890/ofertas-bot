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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

app = Flask(__name__)
EXECUTION_LOCK = threading.Lock()
TOKEN_LOCK = threading.Lock()

class EbayAuthManager:
    def __init__(self):
        self.token_cache = {"access_token": None, "expires_at": 0.0}
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
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
                "Authorization": f"Basic {encoded}"
            }
            # Se elimina el scope para evitar errores de validación en la Browse API
            body = {"grant_type": "client_credentials"}

            resp = self.session.post(url, headers=headers, data=body, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self.token_cache["access_token"] = data.get("access_token")
                self.token_cache["expires_at"] = ahora + data.get("expires_in", 7200)
                return self.token_cache["access_token"]
            else:
                raise Exception(f"Error autenticando eBay: {resp.text}")

    def peticion_con_retry(self, url, headers):
        for intento in range(3):
            try:
                response = self.session.get(url, headers=headers, timeout=(5, 15))
                if response.status_code == 200:
                    time.sleep(random.uniform(0.2, 0.5))
                    return response
                elif response.status_code == 429:
                    time.sleep((2 ** (intento + 1)) + random.uniform(1, 2))
                elif response.status_code >= 500:
                    time.sleep((2 ** (intento + 1)) + 1)
                else:
                    break
            except requests.exceptions.RequestException:
                time.sleep(2)
        return None

ebay_auth = EbayAuthManager()

class MarketAnalyzer:
    @staticmethod
    def validar_item_critico(item):
        for c in ["itemId", "price", "itemWebUrl"]:
            if c not in item: return False
        return True

    @staticmethod
    def es_carta_psa_10(titulo):
        t = titulo.upper()
        if any(bw in t for bw in ["PSA 9", "PSA9", "PSA 8", "PSA8", "RAW", "UNGRADED", "LOT", "BUNDLE", "PACK", "BOX"]):
            return False
        patterns = [r"\bPSA\s*10\b", r"\bPSA\s*GEM\s*MT\s*10\b", r"\bGEM\s*MT\s*10\b", r"\bGEM\s*MINT\s*10\b", r"\bGEM\s*MT\b", r"\bGEM-MT\b"]
        return any(re.search(pat, t) for pat in patterns)

    @staticmethod
    def extraer_info_vendedor_ubicacion(item):
        seller = item.get("seller", {})
        vendedor = seller.get("username", "Desconocido")
        loc = item.get("itemLocation", {})
        location = f"{loc.get('city', '')}, {loc.get('country', 'US')}".strip(", ")
        return vendedor, (location if location else "Estados Unidos")

class EbaySearchEngine:
    QUERIES = ["Lorcana", "Disney Lorcana", "Disney Lorcana Enchanted", "Lorcana PSA"]
    RANGOS = [("0", "150"), ("151", "400"), ("401", "800"), ("801", "1500"), ("1501", "3000"), ("3001", "999999")]

    def buscar_recursivo(self, query, p_min, p_max, sort_order, buying_option, stats):
        items_acumulados = {}
        limit = 100
        category_param = "&category_ids=183454"
        token = ebay_auth.obtener_token()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}

        url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q={query}{category_param}&filter=price:[{p_min}..{p_max}],priceCurrency:USD,buyingOptions:{buying_option}&sort={sort_order}&limit={limit}&offset=0"
        
        resp = ebay_auth.peticion_con_retry(url, headers)
        if not resp or resp.status_code != 200: return items_acumulados

        data = resp.json()
        total_reportado = data.get("total", 0)
        stats["total_reportado"] += total_reportado

        if total_reportado > 2000 and (float(p_max) - float(p_min)) > 1:
            mid = round((float(p_min) + float(p_max)) / 2, 2)
            items_acumulados.update(self.buscar_recursivo(query, p_min, str(mid), sort_order, buying_option, stats))
            items_acumulados.update(self.buscar_recursivo(query, str(mid + 0.01), p_max, sort_order, buying_option, stats))
            return items_acumulados

        paginas = ceil(min(total_reportado, 2000) / limit) if total_reportado > 0 else 1
        for p in range(paginas):
            offset = p * limit
            paged_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q={query}{category_param}&filter=price:[{p_min}..{p_max}],priceCurrency:USD,buyingOptions:{buying_option}&sort={sort_order}&limit={limit}&offset={offset}"
            data_p = data if offset == 0 else ebay_auth.peticion_con_retry(paged_url, headers).json()
            
            for it in data_p.get("itemSummaries", []):
                if MarketAnalyzer.validar_item_critico(it) and MarketAnalyzer.es_carta_psa_10(it.get("title", "")):
                    items_acumulados[it["itemId"]] = it
        return items_acumulados

    def ejecutar_motor_general(self, buying_option, sort_order):
        stats = {"total_reportado": 0}
        todos_items = {}
        for q in self.QUERIES:
            for r in self.RANGOS:
                resultados = self.buscar_recursivo(q, r[0], r[1], sort_order, buying_option, stats)
                todos_items.update(resultados)
        return todos_items, stats

class SheetsManager:
    def __init__(self):
        self.spreadsheet = None

    def conectar(self):
        if self.spreadsheet: return self.spreadsheet
        creds = Credentials.from_service_account_info({
            "type": "service_account",
            "project_id": os.environ.get("GOOGLE_PROJECT_ID"),
            "private_key": os.environ.get("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n"),
            "client_email": os.environ.get("GOOGLE_CLIENT_EMAIL"),
            "token_uri": "https://oauth2.googleapis.com/token"
        }, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
        self.spreadsheet = gspread.authorize(creds).open("Ebay_App")
        return self.spreadsheet

sheets_manager = SheetsManager()
search_engine = EbaySearchEngine()

# ----------------------------------------------------
# ENDPOINT 1: LISTINGS (Buy It Now)
# ----------------------------------------------------
@app.route("/ejecutar-listings", methods=["GET"])
def endpoint_ejecutar_listings():
    if not EXECUTION_LOCK.acquire(blocking=False):
        return jsonify({"status": "busy", "message": "Proceso de listings en ejecución."}), 429
    
    try:
        logging.info("--- [INICIO] Sincronización de Listings Buy It Now ---")
        items, _ = search_engine.ejecutar_motor_general("FIXED_PRICE", "newlyListed")
        
        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_str = datetime.now(tz_cdmx).strftime("%Y-%m-%d %H:%M:%S")
        fecha_solo_dia = datetime.now(tz_cdmx).strftime("%Y-%m-%d")

        sheet = sheets_manager.conectar()
        ws = sheet.worksheet("Listings")
        
        registros = ws.get_all_records()
        ids_existentes_hoy = set()
        
        for row in registros:
            item_id = str(row.get("id_item", ""))
            row_date = str(row.get("date", ""))
            if row_date.startswith(fecha_solo_dia):
                ids_existentes_hoy.add(item_id)

        filas_nuevas = []
        for item_id, it in items.items():
            title = it.get("title", "")
            price = float(it.get("price", {}).get("value", 0))
            url = it.get("itemWebUrl", "")
            vendedor, location = MarketAnalyzer.extraer_info_vendedor_ubicacion(it)
            
            if item_id in ids_existentes_hoy:
                continue
            else:
                filas_nuevas.append([
                    item_id, ahora_str, ahora_str, 1, vendedor, location,
                    "PSA 10", ahora_str, title, price, "Activo", "Buy It Now", price, 0, url
                ])

        if filas_nuevas:
            ws.append_rows(filas_nuevas, value_input_option='USER_ENTERED')
            
        logging.info(f"--- [FIN] Listings procesados. Nuevos agregados: {len(filas_nuevas)} ---")
        return jsonify({"status": "success", "nuevos_agregados": len(filas_nuevas)})
    except Exception as e:
        logging.error(f"Error en listings: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        EXECUTION_LOCK.release()

# ----------------------------------------------------
# ENDPOINT 2: AUCTIONS (Carga diaria + Monitoreo 60s y 2s)
# ----------------------------------------------------
@app.route("/ejecutar-subastas", methods=["GET"])
def endpoint_ejecutar_subastas():
    if not EXECUTION_LOCK.acquire(blocking=False):
        return jsonify({"status": "busy", "message": "Proceso de subastas en ejecución."}), 429

    try:
        logging.info("--- [INICIO] Sincronización y Monitoreo de Subastas ---")
        items, _ = search_engine.ejecutar_motor_general("AUCTION", "endingSoonest")
        
        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        hoy_str = ahora_cdmx.strftime("%Y-%m-%d")

        sheet = sheets_manager.conectar()
        ws = sheet.worksheet("Auctions")
        registros = ws.get_all_records()
        
        ids_en_sheet = {str(r.get("id_item")): (i + 2, r) for i, r in enumerate(registros)}
        
        token = ebay_auth.obtener_token()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}

        filas_nuevas = []
        
        for item_id, it in items.items():
            detalle_resp = ebay_auth.peticion_con_retry(f"https://api.ebay.com/buy/browse/v1/item_summary/{item_id}", headers)
            if not detalle_resp or detalle_resp.status_code != 200:
                continue
            
            detalle = detalle_resp.json()
            closing_time_str = detalle.get("itemEndDate", "")
            
            if not closing_time_str:
                continue
                
            dt_cierre_utc = datetime.fromisoformat(closing_time_str.replace("Z", "+00:00"))
            dt_cierre_cdmx = dt_cierre_utc.astimezone(tz_cdmx)
            closing_date_str = dt_cierre_cdmx.strftime("%Y-%m-%d")
            
            if closing_date_str != hoy_str:
                continue

            title = it.get("title", "")
            precio_inicial = float(it.get("price", {}).get("value", 0))
            url = it.get("itemWebUrl", "")
            vendedor, location = MarketAnalyzer.extraer_info_vendedor_ubicacion(it)
            bids_iniciales = it.get("bidCount", 0)
            
            if item_id in ids_en_sheet:
                row_num, row_data = ids_en_sheet[item_id]
                status_actual = row_data.get("status", "")
                
                if status_actual == "Finalizado":
                    continue
                
                segundos_para_cierre = (dt_cierre_utc - datetime.now(timezone.utc)).total_seconds()
                
                if 45 <= segundos_para_cierre <= 75 and status_actual != "Monitoreado":
                    ws.update_cell(row_num, 8, precio_inicial)
                    ws.update_cell(row_num, 11, bids_iniciales)
                    ws.update_cell(row_num, 14, "Monitoreado")
                    logging.info(f"[Sniper T-60s] Subasta {item_id} actualizada a Monitoreado.")
                
                elif segundos_para_cierre <= 5:
                    ws.update_cell(row_num, 9, precio_inicial)
                    ws.update_cell(row_num, 12, bids_iniciales)
                    ws.update_cell(row_num, 14, "Finalizado")
                    logging.info(f"[Sniper T-2s] Subasta {item_id} finalizada y registrada.")
            
            else:
                filas_nuevas.append([
                    item_id, vendedor, location, "PSA 10", hoy_str, title,
                    precio_inicial, "", "", bids_iniciales, "", "", closing_time_str, "Activo", url
                ])

        if filas_nuevas:
            ws.append_rows(filas_nuevas, value_input_option='USER_ENTERED')
            
        logging.info(f"--- [FIN] Subastas procesadas. Nuevas añadidas: {len(filas_nuevas)} ---")
        return jsonify({"status": "success", "subastas_nuevas": len(filas_nuevas)})
    except Exception as e:
        logging.error(f"Error en subastas: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        EXECUTION_LOCK.release()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
