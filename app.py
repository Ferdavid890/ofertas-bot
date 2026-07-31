import os
import json
import requests
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIGURACIÓN DE GOOGLE SHEETS ---
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = "Ebay_App"

def conectar_sheets():
    try:
        # Si estamos en Render, podemos leer las credenciales desde una variable de entorno 
        # o directamente del archivo json si lo subes al repositorio.
        if os.path.exists("credenciales.json"):
            creds_path = "credenciales.json"
        else:
            # Alternativa segura para la nube si prefieres variable de entorno
            cred_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if cred_json_str:
                creds_dict = json.loads(cred_json_str)
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
                client = gspread.authorize(creds)
                return client.open(SPREADSHEET_NAME)
            raise FileNotFoundError("No se encontró el archivo ni la variable de credenciales.")

        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, SCOPE)
        client = gspread.authorize(creds)
        sheet = client.open(SPREADSHEET_NAME)
        return sheet
    except Exception as e:
        print(f"Error conectando a Google Sheets: {e}")
        return None

# --- LÓGICA DE EXTRACCIÓN DE EBAY (Lorcana PSA 10) ---
def obtener_datos_ebay():
    """
    Aquí conectaremos tus credenciales de la API de eBay (Browse/Finding).
    Por ahora, estructuramos los datos exactamente con las columnas que definiste:
    """
    print("Consultando la API de eBay para Lorcana PSA 10...")
    
    # Ejemplo de datos para Buy It Now (Pestaña: Listings)
    # Columnas: id_item | no_psa | date | title_card | price | listing_type | fmv | volume_7dias
    listings_data = [
        [
            "EBAY-554433", 
            "84592011", 
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
            "Disney Lorcana Mickey Mouse PSA 10", 
            150.00, 
            "Buy It Now", 
            145.00, 
            5
        ]
    ]
    
    # Ejemplo de datos para Subastas (Pestaña: Auctions)
    # Columnas: id_item | no_psa | date | title_card | initial_price | final_price_60s | scheduled_closing_time | status
    auctions_data = [
        [
            "EBAY-998822", 
            "73920192", 
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
            "Disney Lorcana Elsa PSA 10", 
            50.00, 
            0.0, 
            "18:00:00", 
            "Activa"
        ]
    ]
    
    return listings_data, auctions_data

# --- PROCESO PRINCIPAL ---
def ejecutar_proceso():
    sheet = conectar_sheets()
    if not sheet:
        return

    listings, auctions = obtener_datos_ebay()

    # 1. Rellenar pestaña 'Listings' (Buy It Now)
    try:
        ws_listings = sheet.worksheet("Listings")
        for row in listings:
            ws_listings.append_row(row)
        print("¡Datos de Buy It Now agregados a 'Listings' exitosamente!")
    except Exception as e:
        print(f"Error en Listings: {e}")

    # 2. Rellenar pestaña 'Auctions' (Subastas)
    try:
        ws_auctions = sheet.worksheet("Auctions")
        for row in auctions:
            ws_auctions.append_row(row)
        print("¡Datos de Subastas agregados a 'Auctions' exitosamente!")
    except Exception as e:
        print(f"Error en Auctions: {e}")

if __name__ == "__main__":
    ejecutar_proceso()
