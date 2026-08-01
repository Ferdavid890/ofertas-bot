import os
import json
from flask import Flask
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = "Ebay_App"

def conectar_sheets():
    try:
        # Intenta leer las credenciales desde la variable de entorno de Render
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
        else:
            # Fallback por si acaso al archivo local
            creds = ServiceAccountCredentials.from_json_keyfile_name("credenciales.json", SCOPE)
            
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
