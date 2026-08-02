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

def programar_captura_final(item_id, dt_cierre_objetivo):
    try:
        ahora = datetime.now(timezone(timedelta(hours=-6)))
        diferencia_segundos = (dt_cierre_objetivo - ahora).total_seconds() - 60

        if diferencia_segundos > 0:
            time.sleep(diferencia_segundos)

        token = obtener_token_ebay()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        
        item_url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
        response = requests.get(item_url, headers=headers)

        if response.status_code == 200:
            item_data = response.json()
            precio_final = 0.0
            if "currentBidPrice" in item_data:
                precio_final = float(item_data["currentBidPrice"].get("value", 0))
            elif "price" in item_data:
                precio_final = float(item_data["price"].get("value", 0))

            sheet = conectar_sheets()
            ws_auctions = sheet.worksheet("Auctions")
            celda = ws_auctions.find(item_id)

            if celda:
                fila = celda.row
                ws_auctions.update_cell(fila, 6, precio_final)
                ws_auctions.update_cell(fila, 8, "Monitoreada 60s")
    except Exception as e:
        print(f"Error en temporizador para item {item_id}: {str(e)}")

def proceso_fondo():
    try:
        print("=== [INICIO] Proceso de fondo arrancado ===")
        sheet = conectar_sheets()
        print("=== [OK] Conectado a Google Sheets ===")
        
        token = obtener_token_ebay()
        print("=== [OK] Token de eBay obtenido ===")

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        }
        
        ws_listings = sheet.worksheet("Listings")
        ws_auctions = sheet.worksheet("Auctions")

        # Limpiamos las hojas antes de insertar los nuevos datos diarios para evitar duplicados
        ws_listings.clear()
        ws_auctions.clear()

        ws_listings.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "price", "listing_type", "fmv", "volume_7days", "Link"]])
        ws_auctions.update("A1:I1", [["id_item", "no_psa", "date", "title_card", "initial_price", "final_price_60s", "scheduled_closing_time", "status", "Link"]])

        tz_cdmx = timezone(timedelta(hours=-6))
        ahora_cdmx = datetime.now(tz_cdmx)
        hoy_cdmx_str = ahora_cdmx.strftime("%Y-%m-%d")
        fecha_registro_actual = ahora_cdmx.strftime("%Y-%m-%d %H:%M:%S")

        rangos_precios = [
            ("0", "50"), ("50", "100"), ("100", "150"), ("150", "200"),
            ("200", "250"), ("250", "300"), ("300", "350"), ("350", "400"),
            ("400", "450"), ("450", "500"), ("500", "550"), ("550", "600"),
            ("600", "650"), ("650", "700"), ("700", "750"), ("750", "800"),
            ("800", "850"), ("850", "900"), ("900", "950"), ("950", "1000"),
            ("1000", "1100"), ("1100", "1200"), ("1200", "1300"), ("1300", "1400"),
            ("1400", "1500"), ("1500", "1600"), ("1600", "1700"), ("1700", "1800"),
            ("1800", "1900"), ("1900", "2000"), ("2000", "2250"), ("2250", "2500"),
            ("2500", "2750"), ("2750", "3000"), ("3000", "3500"), ("3500", "4000"),
            ("4000", "5000"), ("5000", "999999")
        ]

        todos_los_listings = []
        print("=== Iniciando barrido de Buy It Now por capas (Acumulando en memoria) ===")
        for p_min, p_max in rangos_precios:
            offset = 0
            limit = 100
            while offset < 2000:
                search_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=price:[{p_min}..{p_max}],priceCurrency:USD&limit={limit}&offset={offset}"
                response = requests.get(search_url, headers=headers)
                
                if response.status_code != 200:
                    break
                
                data = response.json()
                items = data.get("itemSummaries", [])
                if not items:
                    break

                for item in items:
                    buying_options = item.get("buyingOptions", [])
                    if "FIXED_PRICE" in buying_options or "BUY_IT_NOW" in buying_options:
                        item_id = item.get("itemId", "")
                        title = item.get("title", "")
                        item_url = item.get("itemWebUrl", "")
                        price_info = item.get("price", {})
                        price = float(price_info.get("value", 0)) if price_info.get("value") else 0.0
                        todos_los_listings.append([item_id, "PSA 10", fecha_registro_actual, title, price, "Buy It Now", price, 1, item_url])

                if len(items) < limit:
                    break
                offset += limit
                time.sleep(0.2)
                gc.collect()

        if todos_los_listings:
            print(f"=== Insertando {len(todos_los_listings)} registros de Buy It Now en lotes ===")
            # Dividir en bloques de 300 para no saturar la API de Google Sheets
            tamano_lote = 300
            for i in range(0, len(todos_los_listings), tamano_lote):
                lote = todos_los_listings[i:i + tamano_lote]
                ws_listings.append_rows(lote, value_input_option='USER_ENTERED')
                time.sleep(1) # Pausa breve entre lotes

        print("=== Iniciando barrido de Subastas ===")
        offset_auc = 0
        todas_las_subastas = []
        subastas_a_monitorear = []

        while offset_auc < 2000:
            auction_url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=Lorcana+PSA+10&filter=buyingOptions:{{AUCTION}},priceCurrency:USD&limit=100&offset={offset_auc}"
            response = requests.get(auction_url, headers=headers)
            if response.status_code != 200:
                break
            data = response.json()
            items = data.get("itemSummaries", [])
            if not items:
                break

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
                            
                            current_bid = 0.0
                            if "currentBidPrice" in item:
                                current_bid = float(item["currentBidPrice"].get("value", 0))
                            elif "price" in item:
                                current_bid = float(item["price"].get("value", 0))
                            
                            cierre_str = dt_cdmx.strftime("%Y-%m-%d %H:%M:%S")

                            todas_las_subastas.append([
                                item_id, "PSA 10", fecha_registro_actual, title, current_bid, 0.0, cierre_str, "Activa", item_url
                            ])

                            subastas_a_monitorear.append((item_id, dt_cdmx))

                    except Exception:
                        continue

            if len(items) < 100:
                break
            offset_auc += 100
            time.sleep(0.2)
            gc.collect()

        if todas_las_subastas:
            ws_auctions.append_rows(todas_las_subastas, value_input_option='USER_ENTERED')
            print(f"=== {len(todas_las_subastas)} subastas insertadas ===")

        for item_id, dt_cdmx in subastas_a_monitorear:
            hilo_monitoreo = threading.Thread(target=programar_captura_final, args=(item_id, dt_cdmx))
            hilo_monitoreo.daemon = True
            hilo_monitoreo.start()

        print("=== PROCESO DE FONDO COMPLETADO EXITOSAMENTE SIN SATURAR GOOGLE ===")

    except Exception as e:
        import traceback
        print("=== ERROR CRÍTICO DETALLADO ===")
        traceback.print_exc()

@app.route("/")
def home():
    return "Bot de eBay para Lorcana PSA 10 operando correctamente 🚀"

@app.route("/ejecutar-freeze-diario", methods=["GET"])
def ejecutar_freeze_diario():
    hilo = threading.Thread(target=proceso_fondo)
    hilo.start()
    return jsonify({
        "status": "success",
        "message": "Sincronización masiva por lotes y monitoreo automático a 60s iniciados en segundo plano."
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
