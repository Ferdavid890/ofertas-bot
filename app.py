import os
import base64
import requests
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
import gspread
from google.oauth2.service_account import Credentials
import threading
import gc

app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = "Ebay_App"

def conectar_sheets():
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
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME)

def obtener_token_ebay():
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
        "scope": "https://oauth2.googleapis.com/oauth/api_scope"
    }

    response = requests.post(url, headers=headers, data=body)
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        raise Exception(f"Error autenticando eBay: {response.text}")

def revisar_y_actualizar_subastas_pendientes():
    """Revisa la hoja de Google Sheets y captura los datos a los 60s y 2s del cierre."""
    try:
        print("Monitoreando subastas en tiempo real...")
        sheet = conectar_sheets()
        ws_auctions = sheet.worksheet("Auctions")
        registros = ws_auctions.get_all_values()

        if len(registros) <= 1:
            return

        tz_cdmx = timezone(timedelta(hours=-6))
        
        token = obtener_token_ebay()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }

        for idx, fila in enumerate(registros[1:], start=2):
            if len(fila) < 12:
                continue
            
            estado = str(fila[11]).strip()
            if estado == "Finalizado":
                continue 

            item_id = str(fila[0]).strip()
            closing_time_str = str(fila[10]).strip()

            if not closing_time_str:
                continue

            try:
                dt_cierre = datetime.strptime(closing_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_cdmx)
            except Exception:
                continue

            ahora_cdmx = datetime.now(tz_cdmx)
            segundos_restantes = (dt_cierre - ahora_cdmx).total_seconds()

            # CASO 1: Captura a los 60 segundos
            price_60s_val = str(fila[5]).strip()
            necesita_60s = (price_60s_val == "" or float(price_60s_val) == 0.0)

            if 0 <= segundos_restantes <= 65 and necesita_60s:
                print(f"Capturando datos 60s antes para subasta: {item_id}")
                item_url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
                response = requests.get(item_url, headers=headers)

                if response.status_code == 200:
                    item_data = response.json()
                    precio_actual = 0.0
                    if "currentBidPrice" in item_data:
                        precio_actual = float(item_data["currentBidPrice"].get("value", 0))
                    elif "price" in item_data:
                        precio_actual = float(item_data["price"].get("value", 0))
                    
                    bids_actual = int(item_data.get("bidCount", 0))

                    ws_auctions.update_cell(idx, 6, precio_actual)  # final_price_60s
                    ws_auctions.update_cell(idx, 9, bids_actual)    # bids_60s
                time.sleep(0.3)

            # CASO 2: Captura a los 2 segundos
            if -2 <= segundos_restantes <= 5:
                if segundos_restantes > 0:
                    time.sleep(segundos_restantes)

                print(f"Capturando datos 2s antes / cierre para subasta: {item_id}")
                item_url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
                response = requests.get(item_url, headers=headers)

                if response.status_code == 200:
                    item_data = response.json()
                    precio_actual = 0.0
                    if "currentBidPrice" in item_data:
                        precio_actual = float(item_data["currentBidPrice"].get("value", 0))
                    elif "price" in item_data:
                        precio_actual = float(item_data["price"].get("value", 0))
                    
                    bids_actual = int(item_data.get("bidCount", 0))

                    ws_auctions.update_cell(idx, 7, precio_actual)  # final_price_2s
                    ws_auctions.update_cell(idx, 10, bids_actual)   # bids_2s
                    ws_auctions.update_cell(idx, 12, "Finalizado")  # status
                time.sleep(0.3)

        print("Monitoreo de subastas completado.")

    except Exception as e:
        print(f"Error en la revisión de subastas: {str(e)}")

def barrido_listings_incremental():
    try:
        print("Ejecutando escaneo incremental de Buy It Now...")
        sheet = conectar_sheets()
        token = obtener_token_ebay()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        
        ws_listings = sheet.worksheet("Listings")
        registros = ws_listings.get_all_values()

        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        hoy_str = ahora_cdmx.strftime("%Y-%m-%d")
        fecha_registro_actual = ahora_cdmx.strftime("%Y-%m-%d %H:%M:%S")

        claves_existentes_hoy = set()
        if len(registros) > 1:
            for fila in registros[1:]:
                if len(fila) >= 3 and fila[0] and fila[2]:
                    item_id = str(fila[0]).strip()
                    fecha_fila = str(fila[2]).strip()[:10]
                    if fecha_fila == hoy_str:
                        claves_existentes_hoy.add(item_id)

        rangos_precios = [
            ("0", "50"), ("51", "100"), ("101", "150"), ("151", "200"),
            ("201", "250"), ("251", "300"), ("301", "350"), ("351", "400"),
            ("401", "450"), ("451", "500"), ("501", "550"), ("551", "600"),
            ("601", "650"), ("651", "700"), ("701", "750"), ("751", "800"),
            ("801", "850"), ("851", "900"), ("901", "950"), ("951", "1000"),
            ("1001", "1100"), ("1101", "1200"), ("1201", "1300"), ("1301", "1400"),
            ("1401", "1500"), ("1501", "1600"), ("1601", "1700"), ("1701", "1800"),
            ("1801", "1900"), ("1901", "2000"), ("2001", "2250"), ("2251", "2500"),
            ("2501", "2750"), ("2751", "3000"), ("3001", "3500"), ("3501", "4000"),
            ("4001", "5000"), ("5001", "999999")
        ]

        nuevos_listings = []
        for p_min, p_max in rangos_precios:
            offset = 0
            limit = 100
            while offset < 2000:
                search_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD&limit={limit}&offset={offset}"
                response = requests.get(search_url, headers=headers)
                if response.status_code != 200:
                    break
                data = response.json()
                items = data.get("itemSummaries", [])
                if not items:
                    break

                for item in items:
                    buying_options = item.get("buyingOptions", [])
                    if "FIXED_PRICE" in buying_options or "BUY_IT_NOW" in buying_options:
                        item_id = str(item.get("itemId", "")).strip()
                        if item_id and item_id not in claves_existentes_hoy:
                            title = item.get("title", "")
                            item_url = item.get("itemWebUrl", "")
                            price_info = item.get("price", {})
                            price = float(price_info.get("value", 0)) if price_info.get("value") else 0.0
                            
                            nuevos_listings.append([item_id, "PSA 10", fecha_registro_actual, title, price, "Buy It Now", price, 1, item_url])
                            claves_existentes_hoy.add(item_id)

                if len(items) < limit:
                    break
                offset += limit
                time.sleep(0.3)
                gc.collect()

        if nuevos_listings:
            ws_listings.append_rows(nuevos_listings, value_input_option='USER_ENTERED')
            print(f"Se agregaron {len(nuevos_listings)} nuevos listings.")

    except Exception as e:
        print(f"Error en escaneo incremental: {str(e)}")

def proceso_fondo():
    try:
        print("Iniciando proceso completo de las 12:01 AM...")
        sheet = conectar_sheets()
        token = obtener_token_ebay()

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        
        ws_listings = sheet.worksheet("Listings")
        ws_auctions = sheet.worksheet("Auctions")

        ws_listings.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "price", "listing_type", "fmv", "volume_7days", "Link"]])
        ws_auctions.update("A1:M1", [["id_item", "no_psa", "date", "title_card", "initial_price", "final_price_60s", "final_price_2s", "bids", "bids_60s", "bids_2s", "scheduled_closing_time", "status", "Link"]])

        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        hoy_cdmx_str = ahora_cdmx.strftime("%Y-%m-%d")
        fecha_registro_actual = ahora_cdmx.strftime("%Y-%m-%d %H:%M:%S")

        registros_existentes_listings = ws_listings.get_all_values()
        ids_vistos_hoy = set()
        if len(registros_existentes_listings) > 1:
            for fila in registros_existentes_listings[1:]:
                if len(fila) >= 3 and fila[0] and fila[2]:
                    item_id = str(fila[0]).strip()
                    fecha_fila = str(fila[2]).strip()[:10]
                    if fecha_fila == hoy_cdmx_str:
                        ids_vistos_hoy.add(item_id)

        rangos_precios = [
            ("0", "50"), ("51", "100"), ("101", "150"), ("151", "200"),
            ("201", "250"), ("251", "300"), ("301", "350"), ("351", "400"),
            ("401", "450"), ("451", "500"), ("501", "550"), ("551", "600"),
            ("601", "650"), ("651", "700"), ("701", "750"), ("751", "800"),
            ("801", "850"), ("851", "900"), ("901", "950"), ("951", "1000"),
            ("1001", "1100"), ("1101", "1200"), ("1201", "1300"), ("1301", "1400"),
            ("1401", "1500"), ("1501", "1600"), ("1601", "1700"), ("1701", "1800"),
            ("1801", "1900"), ("1901", "2000"), ("2001", "2250"), ("2251", "2500"),
            ("2501", "2750"), ("2751", "3000"), ("3001", "3500"), ("3501", "4000"),
            ("4001", "5000"), ("5001", "999999")
        ]

        # 1. BARRIDO LISTINGS
        for p_min, p_max in rangos_precios:
            offset = 0
            limit = 100
            while offset < 2000:
                search_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD&limit={limit}&offset={offset}"
                response = requests.get(search_url, headers=headers)
                if response.status_code != 200:
                    break
                data = response.json()
                items = data.get("itemSummaries", [])
                if not items:
                    break

                listings_lote = []
                for item in items:
                    buying_options = item.get("buyingOptions", [])
                    if "FIXED_PRICE" in buying_options or "BUY_IT_NOW" in buying_options:
                        item_id = str(item.get("itemId", "")).strip()
                        if item_id and item_id not in ids_vistos_hoy:
                            ids_vistos_hoy.add(item_id)
                            title = item.get("title", "")
                            item_url = item.get("itemWebUrl", "")
                            price_info = item.get("price", {})
                            price = float(price_info.get("value", 0)) if price_info.get("value") else 0.0
                            listings_lote.append([item_id, "PSA 10", fecha_registro_actual, title, price, "Buy It Now", price, 1, item_url])

                if listings_lote:
                    ws_listings.append_rows(listings_lote, value_input_option='USER_ENTERED')

                if len(items) < limit:
                    break
                offset += limit
                time.sleep(0.3)
                gc.collect()

        # 2. BARRIDO AUCTIONS
        offset_auc = 0
        ids_vistos_subastas = set()
        while offset_auc < 2000:
            auction_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=buyingOptions:{{AUCTION}},priceCurrency:USD&limit=100&offset={offset_auc}"
            response = requests.get(auction_url, headers=headers)
            if response.status_code != 200:
                break
            data = response.json()
            items = data.get("itemSummaries", [])
            if not items:
                break

            auctions_lote = []
            for item in items:
                buying_options = item.get("buyingOptions", [])
                if "AUCTION" in buying_options:
                    item_end_time = item.get("itemEndDate", "")
                    if item_end_time:
                        try:
                            dt_utc = datetime.fromisoformat(item_end_time.replace("Z", "+00:00"))
                            dt_cdmx = dt_utc.astimezone(tz_cdmx)
                            fecha_cierre_cdmx_str = dt_cdmx.strftime("%Y-%m-%d")

                            if fecha_cierre_cdmx_str == hoy_cdmx_str:
                                item_id = str(item.get("itemId", "")).strip()
                                if item_id and item_id not in ids_vistos_subastas:
                                    ids_vistos_subastas.add(item_id)
                                    title = item.get("title", "")
                                    item_url = item.get("itemWebUrl", "")
                                    
                                    current_bid = 0.0
                                    if "currentBidPrice" in item:
                                        current_bid = float(item["currentBidPrice"].get("value", 0))
                                    elif "price" in item:
                                        current_bid = float(item["price"].get("value", 0))
                                    
                                    initial_bids = int(item.get("bidCount", 0))
                                    cres_str = dt_cdmx.strftime("%Y-%m-%d %H:%M:%S")

                                    auctions_lote.append([
                                        item_id, "PSA 10", fecha_registro_actual, title, current_bid, 0.0, 0.0, initial_bids, 0, 0, cres_str, "Activa", item_url
                                    ])
                        except Exception:
                            continue

            if auctions_lote:
                ws_auctions.append_rows(auctions_lote, value_input_option='USER_ENTERED')

            if len(items) < 100:
                break
            offset_auc += 100
            time.sleep(0.3)
            gc.collect()

    except Exception as e:
        print(f"ERROR CRÍTICO: {str(e)}")

@app.route("/ping")
def ping():
    return "Pong! Servidor activo.", 200

@app.route("/")
def home():
    return "Bot de eBay operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    hilo = threading.Thread(target=proceso_fondo)
    hilo.start()
    return jsonify({"status": "success", "message": "Barrido diario iniciado."})

@app.route("/actualizar-listings-nuevos", methods=["GET"])
def actualizar_listings_nuevos():
    hilo = threading.Thread(target=barrido_listings_incremental)
    hilo.start()
    return jsonify({"status": "success", "message": "Incremental iniciado."})

@app.route("/revisar-subastas", methods=["GET"])
def revisar_subastas():
    hilo = threading.Thread(target=revisar_y_actualizar_subastas_pendientes)
    hilo.start()
    return jsonify({"status": "success", "message": "Monitoreo de subastas iniciado."})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
