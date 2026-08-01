import os
from flask import Flask
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# --- CONFIGURACIÓN DE GOOGLE SHEETS ---
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = "Ebay_App"

def conectar_sheets():
    try:
        if os.path.exists("credenciales.json"):
            creds_path = "credenciales.json"
            creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, SCOPE)
            client = gspread.authorize(creds)
            return client.open(SPREADSHEET_NAME)
        return None
    except Exception as e:
        print(f"Error conectando a Google Sheets: {e}")
        return None

@app.route("/")
def home():
    # Intento de prueba al entrar a la web
    sheet = conectar_sheets()
    if sheet:
        try:
            ws_listings = sheet.worksheet("Listings")
            ws_listings.append_row([
                "EBAY-TEST", "000000", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Test Card PSA 10", 100.0, "Buy It Now", 100.0, 1
            ])
            return "¡Bot conectado y registro de prueba enviado a Google Sheets con éxito!"
        except Exception as e:
            return f"Conectado a Sheets pero hubo un error escribiendo: {e}"
    return "Servidor activo, pero revisa el archivo credenciales.json"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
