import os
import base64
import requests
from datetime import datetime
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
    try:
        client_email = os.environ.get("GOOGLE_CLIENT_EMAIL")
        private_key = os.environ.get("GOOGLE_PRIVATE_KEY")
        project_id = os.environ.get("GOOGLE_PROJECT_ID")

        if not client_email or not private_key or not project_id:
            return None

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
    except Exception as e:
        print(f"Error conectando a Sheets: {e}")
        return None

def obtener_token_ebay():
    """Genera el token de acceso OAuth para consumir la API de eBay"""
    client_id = os.environ.get("EBAY_CLIENT_ID")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET")

    if not client_id or not client_secret:
        return None

    credentials = f"{client_id}:{client_secret}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_credentials}"
    }
    body = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }

    response = requests.post(url, headers=headers, data=body)
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        print(f"Error obteniendo token de eBay: {response.text}")
        return None

@app.route("/")
def home():
    return "Bot de eBay para Lorcana PSA 10 operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    sheet = conectar_sheets()
    if not sheet:
        return jsonify({"status": "error", "message": "No se pudo conectar a Google Sheets"}), 500

    token = obtener_token_ebay()
    if not token:
        return jsonify({"status": "error", "message": "No se pudo autenticar con la API de eBay. Revisa tus credenciales."}), 500

    try:
        # Llamada a la Browse API de eBay buscando "Lorcana PSA 10"
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        # Buscamos artículos activos de Lorcana PSA 10
        search_url = "https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&limit=50"
        
        response = requests.get(search_url, headers=headers)
        if response.status_code != 200:
            return jsonify({"status": "error", "message": f"Error en la API de eBay: {response.text}"}), 500

        data = response.json()
        items = data.get("itemSummaries", [])

        ws_listings = sheet.worksheet("Listings")
        ws_listings.clear() # Limpiamos y reescribimos encabezados si es necesario o appendeamos
        # Aseguramos encabezados por si acaso
        ws_listings.update("A1:H1", [["id_item", "no_psa", "date", "title_card", "price", "listing_type", "fmv", "volume_7days"]])

        ws_auctions = sheet.worksheet("Auctions")
        
        listings_agregados = 0
        auctions_agregadas = 0

        for item in items:
            item_id = item.get("itemId", "")
            title = item.get("title", "")
            
            # Intentar extraer precio
            price_info = item.get("price", {})
            price = float(price_info.get("value", 0))
            
            # Determinar tipo de compra (Buy It Now o Subasta)
            buying_options = item.get("buyingOptions", [])
            
            # Fecha actual del registro
            fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if "AUCTION" in buying_options:
                # Es subasta -> va a la pestaña Auctions
                # Obtenemos la hora de fin si la API la provee
                item_end_date = item.get("itemEndDate", "")
                ws_auctions.append_row([
                    item_id, "PSA 10", fecha_actual, title, price, 0.0, item_end_date, "Pending"
                ])
                auctions_agregadas += 1
            else:
                # Es Buy It Now -> va a la pestaña Listings
                ws_listings.append_row([
                    item_id, "PSA 10", fecha_actual, title, price, "Buy It Now", price, 1
                ])
                listings_agregados += 1

        return jsonify({
            "status": "success",
            "message": f"Freeze completado con éxito. Buy It Now guardados: {listings_agregados}, Subastas guardadas: {auctions_agregadas}"
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
