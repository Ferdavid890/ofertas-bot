import os
import json
from flask import Flask
from datetime import datetime
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
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            # Carga el JSON asegurando que los saltos de línea de la llave privada funcionen bien
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file("credenciales.json", scopes=SCOPES)
            
        client = gspread.authorize(creds)
        return client.open(SPREADSHEET_NAME)
    except Exception as e:
        print(f"Error detallado Google Sheets: {e}")
        return None

@app.route("/")
def home():
    sheet = conectar_sheets()
    if sheet:
        try:
            ws_listings = sheet.worksheet("Listings")
            ws_listings.append_row([
                "EBAY-TEST", "123456", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Prueba Exitosa Render", 150.0, "Buy It Now", 150.0, 1
            ])
            return "¡Conexión exitosa! Revisa tu Google Sheets, el registro de prueba ya fue enviado."
        except Exception as e:
            return f"Conectado al archivo, pero error en la tabla: {e}"
    return "Error de firma o credenciales. Revisa los logs de Render."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
