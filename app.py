import os
import base64
import requests
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
import gspread
from google.oauth2.service_account import Credentials
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

@app.route("/")
def home():
    return "Bot de eBay para Lorcana PSA 10 operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    try:
        sheet = conectar_sheets()
        token = obtener_token_ebay()

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        
        ws_listings = sheet.worksheet("Listings")
        ws_auctions = sheet.worksheet("Auctions")

        if len(ws_listings.get_all_values()) == 0:
            ws_listings.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "price", "listing_type", "fmv", "volume_7days", "Link"]])

        if len(ws_auctions.get_all_values()) == 0:
            ws_auctions.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "initial_price", "final_price_60s", "scheduled_closing_time", "status", "Link"]])

        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        fecha_registro_actual = ahora_cdmx.strftime("%Y-%m-%d %H:%M:%S")

        # Usamos un diccionario de parámetros para que requests maneje la URL de forma impecable
        search_url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
        params = {
            "q": "Lorcana PSA 10",
            "filter": "price:[0..100],priceCurrency:USD",
            "limit": 10
        }
        
        response = requests.get(search_url, headers=headers, params=params)
        
        if response.status_code != 200:
            return jsonify({"status": "error", "detail": f"Error eBay: {response.text}"})

        data = response.json()
        items = data.get("itemSummaries", [])
        
        listings_lote = []
        for item in items:
            item_id = item.get("itemId", "")
            title = item.get("title", "")
            item_url = item.get("itemWebUrl", "")
            price_info = item.get("price", {})
            price = float(price_info.get("value", 0)) if price_info.get("value") else 0.0
            listings_lote.append([item_id, "PSA 10", fecha_registro_actual, title, price, "Buy It Now", price, 1, item_url])

        if listings_lote:
            ws_listings.append_rows(listings_lote, value_input_option='USER_ENTERED')

        return jsonify({
            "status": "success",
            "message": f"¡Conexión y consulta exitosas! Se insertaron {len(listings_lote)} elementos. Revisa tu Google Sheet."
        })

    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
