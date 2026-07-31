from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import datetime

app = Flask(__name__)

def tarea_escaneo():
    # Aquí irá tu lógica para escanear y guardar en Google Sheets
    print(f"Ejecutando escaneo automático a las: {datetime.datetime.now()}")

# Configurar el temporizador en segundo plano
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_escaneo, trigger="interval", minutes=5)
scheduler.start()

@app.route("/")
def home():
    return "El bot de ofertas está activo y funcionando perfectamente."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
