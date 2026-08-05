import os
import base64
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
import gspread
from google.oauth2.service_account import Credentials

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
    # Scope requerido por la API de eBay para autorizar las consultas de Buy Browse
    body = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }

    response = requests.post(url, headers=headers, data=body)
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        raise Exception(f"Error autenticando eBay: {response.text}")

def proceso_prueba_5_registros():
    print("Iniciando prueba de descarga de 5 registros...")
    sheet = conectar_sheets()
    token = obtener_token_ebay()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
    }
    
    ws_listings = sheet.worksheet("Listings")

    if len(ws_listings.get_all_values()) == 0:
        ws_listings.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "price", "listing_type", "fmv", "volume_7days", "Link"]])

    tz_cdmx = timezone(timedelta(hours=-6))
    ahora_cdmx = datetime.now(tz_cdmx)
    fecha_registro_actual = ahora_cdmx.strftime("%Y-%m-%d %H:%M:%S")

    search_url = "https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&limit=5"
    response = requests.get(search_url, headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Error en la API de eBay: {response.text}")

    data = response.json()
    items = data.get("itemSummaries", [])
    
    if not items:
        raise Exception("La API respondió correctamente pero no devolvió ningún artículo para 'Lorcana PSA 10'.")

    listings_lote = []
    for item in items:
        item_id = str(item.get("itemId", "")).strip()
        title = item.get("title", "")
        item_url = item.get("itemWebUrl", "")
        price_info = item.get("price", {})
        price = float(price_info.get("value", 0)) if price_info.get("value") else 0.0
        
        listings_lote.append([item_id, "PSA 10", fecha_registro_actual, title, price, "Buy It Now", price, 1, item_url])

    if listings_lote:
        ws_listings.append_rows(listings_lote, value_input_option='USER_ENTERED')
        print(f"Se agregaron {len(listings_lote)} registros exitosamente.")

@app.route("/")
def home():
    return "Bot de eBay operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    try:
        proceso_prueba_5_registros()
        return jsonify({
            "status": "success",
            "message": "¡Prueba de 5 registros insertada con éxito en Google Sheets!"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
