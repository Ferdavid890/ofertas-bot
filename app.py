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
        "scope": "https://api.ebay.com/oauth/api_scope/buy.item.bulk"
    }

    response = requests.post(url, headers=headers, data=body)
    if response.status_code != 200:
        body["scope"] = "https://api.ebay.com/oauth/api_scope"
        response = requests.post(url, headers=headers, data=body)

    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        raise Exception(f"Error autenticando eBay: {response.text}")

def programar_captura_final(item_id, dt_cierre_objetivo):
    try:
        ahora = datetime.now(timezone(timedelta(hours=-6)))
        diferencia_60s = (dt_cierre_objetivo - ahora).total_seconds() - 60
        if diferencia_60s > 0:
            time.sleep(diferencia_60s)

        token = obtener_token_ebay()
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        item_url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
        response = requests.get(item_url, headers=headers)

        if response.status_code == 200:
            item_data = response.json()
            precio_60s = float(item_data.get("currentBidPrice", item_data.get("price", {})).get("value", 0))
            bids_60s = int(item_data.get("bidCount", 0))

            sheet = conectar_sheets()
            ws_auctions = sheet.worksheet("Auctions")
            celda = ws_auctions.find(item_id)
            if celda:
                fila = celda.row
                ws_auctions.update_cell(fila, 6, precio_60s)
                ws_auctions.update_cell(fila, 9, bids_60s)
                ws_auctions.update_cell(fila, 12, "Monitoreado 60s")

        time.sleep(58)
        token = obtener_token_ebay()
        headers["Authorization"] = f"Bearer {token}"
        response_2s = requests.get(item_url, headers=headers)
        if response_2s.status_code == 200:
            item_data_2s = response_2s.json()
            precio_2s = float(item_data_2s.get("currentBidPrice", item_data_2s.get("price", {})).get("value", 0))
            bids_2s = int(item_data_2s.get("bidCount", 0))

            sheet = conectar_sheets()
            ws_auctions = sheet.worksheet("Auctions")
            celda = ws_auctions.find(item_id)
            if celda:
                fila = celda.row
                ws_auctions.update_cell(fila, 7, precio_2s)
                ws_auctions.update_cell(fila, 10, bids_2s)
                ws_auctions.update_cell(fila, 12, "Finalizado")
    except Exception as e:
        print(f"Error en temporizador para item {item_id}: {str(e)}")

def proceso_fondo():
    try:
        print("Iniciando proceso completo y seguro de descarga...")
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

        # Rangos de precios seguros y optimizados para evitar bloqueos
        rangos_precios = [
            ("0", "100"), ("101", "250"), ("251", "500"), 
            ("501", "1000"), ("1001", "2500"), ("2501", "999999")
        ]

        ids_vistos_hoy = set()
        total_listings_agregados = 0

        for p_min, p_max in rangos_precios:
            offset = 0
            limit = 100
            print(f"Scrapeando rango de precios: ${p_min} - ${p_max}")
            while offset < 1000:
                search_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD&limit={limit}&offset={offset}"
                
                intentos = 0
                response = None
                while intentos < 5:
                    response = requests.get(search_url, headers=headers)
                    if response.status_code == 429:
                        intentos += 1
                        tiempo_espera = 3 * intentos
                        print(f"Límite 429 en ${p_min}-${p_max}. Esperando {tiempo_espera}s...")
                        time.sleep(tiempo_espera)
                    else:
                        break

                if response.status_code != 200:
                    print(f"Aviso: Rango ${p_min}-${p_max} devolvió HTTP {response.status_code}")
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
                    total_listings_agregados += len(listings_lote)
                    print(f"-> Agregados {len(listings_lote)} items del rango ${p_min}-${p_max}")

                if len(items) < limit:
                    break
                offset += limit
                time.sleep(1.0)
            time.sleep(1.5)
            gc.collect()

        print(f"Total de Listings agregados hoy: {total_listings_agregados}")

        # Sección de Subastas (Auctions)
        offset_auc = 0
        ids_vistos_subastas = set()
        total_auctions_agregadas = 0
        print("Iniciando descarga de subastas...")
        while offset_auc < 1000:
            auction_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=priceCurrency:USD&limit=100&offset={offset_auc}"
            
            intentos = 0
            response = None
            while intentos < 5:
                response = requests.get(auction_url, headers=headers)
                if response.status_code == 429:
                    intentos += 1
                    time.sleep(3 * intentos)
                else:
                    break

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
                                    current_bid = float(item.get("currentBidPrice", item.get("price", {})).get("value", 0))
                                    initial_bids = int(item.get("bidCount", 0))
                                    cierre_str = dt_cdmx.strftime("%Y-%m-%d %H:%M:%S")

                                    auctions_lote.append([
                                        item_id, "PSA 10", fecha_registro_actual, title, current_bid, 0.0, 0.0, initial_bids, 0, 0, cierre_str, "Activa", item_url
                                    ])

                                    hilo_monitoreo = threading.Thread(target=programar_captura_final, args=(item_id, dt_cdmx))
                                    hilo_monitoreo.daemon = True
                                    hilo_monitoreo.start()
                        except Exception:
                            continue

            if auctions_lote:
                ws_auctions.append_rows(auctions_lote, value_input_option='USER_ENTERED')
                total_auctions_agregadas += len(auctions_lote)

            if len(items) < 100:
                break
            offset_auc += 100
            time.sleep(1.0)
            gc.collect()

        print(f"Total de Auctions agregadas hoy: {total_auctions_agregadas}")
        print("¡Sincronización completada con éxito!")

    except Exception as e:
        print(f"ERROR CRÍTICO en proceso de fondo: {str(e)}")

@app.route("/ping")
def ping():
    return "Pong! Servidor activo.", 200

@app.route("/")
def home():
    return "Bot de eBay para Lorcana PSA 10 operando correctamente 🚀"

@app.route("/probar-directo", methods=["GET"])
def probar_directo():
    hilo = threading.Thread(target=proceso_fondo)
    hilo.daemon = True
    hilo.start()
    return jsonify({
        "status": "success",
        "message": "¡Proceso iniciado en segundo plano! Revisa tus logs en unos momentos y tu Google Sheets."
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
