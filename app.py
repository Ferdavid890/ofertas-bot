import json
import os
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
        credentials_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not credentials_json_str:
            print("Error: La variable de entorno GOOGLE_CREDENTIALS_JSON no está configurada.")
            return None
            
        # Limpiar comillas extras si por error las pusieron al copiar en Render
        credentials_json_str = credentials_json_str.strip()
        if credentials_json_str.startswith("'") and credentials_json_str.endswith("'"):
            credentials_json_str = credentials_json_str[1:-1]
        elif credentials_json_str.startswith('"') and credentials_json_str.endswith('"'):
            credentials_json_str = credentials_json_str[1:-1]

        creds_info = json.loads(credentials_json_str)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
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
    return "Error de credenciales o formato JSON. Revisa los logs en Render."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
