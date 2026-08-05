import os
import time
import gc
import requests
from datetime import datetime, timezone, timedelta
import threading
import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, jsonify

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

    auth_url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }
    
    response = requests.post(auth_url, headers=headers, data=data, auth=(client_id, client_secret))
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        raise Exception(f"No se pudo obtener el token de eBay: {response.text}")

def programar_captura_final(item_id, dt_cierre_cdmx):
    pass

def proceso_fondo():
    try:
        print("Iniciando proceso completo y masivo por capas...")
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

        total_listings_agregados = 0
        for p_min, p_max in rangos_precios:
            offset = 0
            limit = 100
            while offset < 1000:
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
                    total_listings_agregados += len(listings_lote)

                if len(items) < limit:
                    break
                offset += limit
                time.sleep(0.1)
                gc.collect()

        print(f"Total de Listings agregados hoy: {total_listings_agregados}")

        ids_vistos_subastas = set()
        total_auctions_agregadas = 0
        
        for p_min, p_max in rangos_precios:
            offset_auc = 0
            while offset_auc < 1000:
                auction_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=buyingOptions:{{AUCTION}},price:[{p_min}..{p_max}],priceCurrency:USD&limit=100&offset={offset_auc}"
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

                                        hilo_monitoreo = threading.Thread(target=programar_captura_final, args=(item_id, dt_cdmx))
                                        hilo_monitoreo.daemon = True
                                        hilo_monitoreo.start()

                            except Exception as ex_item:
                                continue

                if auctions_lote:
                    ws_auctions.append_rows(auctions_lote, value_input_option='USER_ENTERED')
                    total_auctions_agregadas += len(auctions_lote)

                if len(items) < 100:
                    break
                offset_auc += 100
                time.sleep(0.1)
                gc.collect()

        print(f"Total de Auctions agregadas hoy: {total_auctions_agregadas}")

    except Exception as e:
        print(f"ERROR CRÍTICO en proceso de fondo: {str(e)}")

@app.route('/ejecutar-freeze-diario', methods=['GET'])
def disparar_proceso():
    hilo = threading.Thread(target=proceso_fondo)
    hilo.daemon = True
    hilo.start()
    return jsonify({"message": "Sincronización masiva de las 12:01 AM iniciada por capas.", "status": "success"})

@app.route('/ping', methods=['GET'])
def ping():
    return "Pong", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
