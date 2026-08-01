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

# JSON de credenciales integrado directamente y blindado contra errores de formato
CREDENTIALS_DICT = json.loads("""{
  "type": "service_account",
  "project_id": "ebaybotapp",
  "private_key_id": "e9a0968cc0f4716f04d7f1be34ae2666c94cf65e",
  "private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDeHr+XX9mNzuXW\\nLaJvDLhLmZ75t3Jou5TOmD3qdpo8/49C6VhlEo2hqTQh1Y/03mhmozEN+cyeAJE+\\nvTjH0BNu8YE2R3hCg61UI+XCY2xCPuRTg+dgdF2j7z3EHkdHhaui6l65rA5qwbyq\\neS+teC/kiPfQuw/XMFiZWM6YfMuHQX/M5PI2c9TygsTYJPbxgcFGfgtEujlNlHmg\\nQHYXPi4SRNf9BnP0eRUiVFUaSD7cFiKWLuPv6C2XEHF4SXEJdhnJQzpHJy+siLz2\\nYjVy6naY67EV3WBa9pbq/dUh+Xi/YwkIy1OaBy9d4+UrguudbiWA8qbfFrxNXty9\\nMUZBNBzDAgMBAAECggEAE0cn6cNv5lbmq8gaKPk5pZYXriS10VE2gRfFh+vzRwgH\\nLw+BlIQftsAwvh8C94W2GfJf946Oq8fw0zkpDG6KwT5EsKlTTrKPAJZ9AnoOk1FS\\nD82K71wqJGhHPBZEqXh4hRNCVWsRdUKLVWBfOvcLcRJSL9OMdGjFx8llZOav43Uq\\ndSXFe70FmqooVE8P3KQX2o6FKVDv6mJ1Za5Hin/qWVyhRaLoSVjmvNkq41q8ce0n\\nFTp6PSrWAyUF149ULNRxXRW6obffnN27/O62yuZdZImg0b2wsQ6YsWzXsN9hUL8x\\nAmxj8uLdSkmfncMMu2+Ig+2Civij6lXsSssHgQWJwQKBgQDxQ24OAyeC2lcIzDNF\\n3fBJNX5YXnKwNY1EcX0PmNTz2x4RF+E8DhrElf9lNnukqgcdr3K7PS6BSB133YeS\\ne4naatfHNlBgkptYhgwpXmif+wBpPSPObbUu4fmVOwLGqj1JeKMlIy3H8uLuBRrI\\n7jIDwfWtOrSGWcQhE4M5T8JZvwKBgQDrr/rp5LFvohVGvMJWLPRIPVviy1xdBHpv\\ngWkmupCdiJH0hS6sXdqHvPGSyrPEeOr31QWiacP4uzCADy7MXUmVWn/8RaeU6z1J\\nUIeROnDnDIAaU+5jiTEgBeMS/31CGXIATPGGtemZO+KikIDd+1/1qdVwFDfGM0Br\\nsrY6fOtV/QKBgDNCREut1+MxSHSSDgK2GKs1NlbIGk3d0tnL0upRak01LLos/Kmp\\nxX4m8FAstzBQ/5oLALFPWmYVUE17P6aboLpLIPUuUP1zqJWyRTs0173FslyppMXj\\nAS+oy0Ite3WCDetiOidVxhBJRnWTmBFAqleqCex4IIq637S3VJYEoCI5AoGBANGv\\nwNnFGMQL/Ufw+il3V2LKDG0LpsIvEMsR5L6LL8yoS8qzjyHVYm5vgLGr3CJJvir+\\ngEPOO4eY6v6UA3vY53WUjdehFQad/+mxVtuzle1KJtLFp4sw7N7jvfISEpvzTYTM\\n7/l88Tbem7UsQSq90dMb5YQQyMpyoLbwycXhi/L1AoGAYqL8ebnAHC/JJo6tFHXQ\\nd2qegC6DUUdC68K63PJ1mw6xuDB9qJhpVf9Dg/UMRpEZxyW3MIkPB/rVsAjAJ6on\\nDGgssEnha4cNbH/wyj6gvKd42gfc2EsMK5lDr68k6GlRFHZhTG7YXP3fOtKOfNUp\\Ese6MpVS2fzWixxjIdrO+3k=\\n-----END PRIVATE KEY-----"
}""")

def conectar_sheets():
    try:
        creds = Credentials.from_service_account_info(CREDENTIALS_DICT, scopes=SCOPES)
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
