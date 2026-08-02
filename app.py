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
        "grant_type": "client_credentials"
    }

    response = requests.post(url, headers=headers, data=body)
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        raise Exception(f"Error autenticando eBay: {response.text}")

def proceso_fondo():
    try:
        print("Iniciando proceso en segundo plano...")
        sheet = conectar_sheets()
        token = obtener_token_ebay()

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        
        ws_listings = sheet.worksheet("Listings")
        ws_auctions = sheet.worksheet("Auctions")

        if len(ws_listings.get_all_values()) == 0:
            ws_listings.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "price", "listing_type", "fmv", "volume_7days", "Link"]])

        if len(ws_auctions.get_all_values()) == 0:
            ws_auctions.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "initial_price", "final_price_60s", "scheduled_closing_time", "status", "Link"]])

        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        hoy_cdmx_str = ahora_cdmx.strftime("%Y-%m-%d")
        fecha_registro_actual = ahora_cdmx.strftime("%Y-%m-%d %H:%M:%S")

        # Rangos optimizados y limpios para evitar errores de sintaxis en la URL
        rangos_precios = [
            ("0", "100"),
            ("101", "200"),
            ("201", "300"),
            ("301", "400"),
            ("401", "500"),
            ("501", "600"),
            ("601", "700"),
            ("701", "800"),
            ("801", "900"),
            ("901", "1000"),
            ("1001", "1100"),
            ("1101", "1200"),
            ("1201", "1300"),
            ("1301", "1400"),
            ("1401", "1500"),
            ("1501", "1600"),
            ("1601", "1700"),
            ("1701", "1800"),
            ("1801", "1900"),
            ("1901", "2000"),
            ("2001", "2500"),
            ("2501", "3000"),
            ("3001", "3500"),
            ("3501", "4000"),
            ("4001", "5000"),
            ("5001", "999999")
        ]

        print("Barrido de Buy It Now por capas iniciado...")
        for p_min, p_max in rangos_precios:
            offset = 0
            limit = 100
            
            while offset < 2000:
                search_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD&limit={limit}&offset={offset}"
                response = requests.get(search_url, headers=headers)
                
                if response.status_code != 200:
                    print(f"Aviso en rango {p_min}-{p_max}: {response.text}")
                    break

                data = response.json()
                items = data.get("itemSummaries", [])
                
                if not items:
                    break

                listings_lote = []
                for item in items:
                    buying_options = item.get("buyingOptions", [])
                    if "FIXED_PRICE" in buying_options or "BUY_IT_NOW" in buying_options:
                        item_id = item.get("itemId", "")
                        title = item.get("title", "")
                        item_url = item.get("itemWebUrl", "")
                        price_info = item.get("price", {})
                        price = float(price_info.get("value", 0)) if price_info.get("value") else 0.0

                        listings_lote.append([
                            item_id, "PSA 10", fecha_registro_actual, title, price, "Buy It Now", price, 1, item_url
                        ])

                if listings_lote:
                    ws_listings.append_rows(listings_lote, value_input_option='USER_ENTERED')

                if len(items) < limit:
                    break

                offset += limit
                time.sleep(0.3)
                gc.collect()

        print("Barrido de Subastas iniciado...")
        offset_auc = 0
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
                item_end_time = item.get("itemEndDate", "")
                if item_end_time:
                    try:
                        dt_utc = datetime.fromisoformat(item_end_time.replace("Z", "+00:00"))
                        dt_cdmx = dt_utc.astimezone(tz_cdmx)
                        fecha_cierre_cdmx_str = dt_cdmx.strftime("%Y-%m-%d")

                        if fecha_cierre_cdmx_str == hoy_cdmx_str:
                            item_id = item.get("itemId", "")
                            title = item.get("title", "")
                            item_url = item.get("itemWebUrl", "")
                            
                            price_info = item.get("price", {})
                            current_bid = float(price_info.get("value", 0)) if price_info.get("value") else 0.0
                            
                            cierre_str = dt_cdmx.strftime("%Y-%m-%d %H:%M:%S")

                            auctions_lote.append([
                                item_id, "PSA 10", fecha_registro_actual, title, current_bid, 0.0, cierre_str, "Activa", item_url
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

        print("Sincronización total de 100 en 100 finalizada con éxito.")

    except Exception as e:
        print(f"Error crítico en proceso de fondo: {str(e)}")

@app.route("/")
def home():
    return "Bot de eBay para Lorcana PSA 10 operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    hilo = threading.Thread(target=proceso_fondo)
    hilo.start()
    return jsonify({
        "status": "success",
        "message": "Sincronización con rangos limpios de 100 en 100 iniciada en segundo plano."
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
