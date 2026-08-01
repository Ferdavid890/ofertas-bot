import os
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

@app.route("/")
def home():
    return "Bot de eBay para Lorcana PSA 10 operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    sheet = conectar_sheets()
    if not sheet:
        return jsonify({"status": "error", "message": "No se pudo conectar a Google Sheets"}), 500

    try:
        ws_listings = sheet.worksheet("Listings")
        ws_listings.append_row([
            "EBAY-LORCANA-001", "12345678", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Lorcana PSA 10 Test Card", 250.0, "Buy It Now", 250.0, 5
        ])
        return jsonify({"status": "success", "message": "Freeze registrado correctamente en Listings."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
