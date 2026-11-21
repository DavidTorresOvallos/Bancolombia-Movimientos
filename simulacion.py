# -*- coding: utf-8 -*-
# =====================================================================
# SIMULACION.PY  (10/10/2026)
# Modo simulación para pruebas de carga del SISTEMA REAL.
#
# Idea: correr iniciar.py tal cual (Chrome real, cuentas reales,
# navegación real = carga real de CPU), pero de forma AISLADA:
#   - Las notificaciones de Telegram ya van al bot de PRUEBA porque se
#     corre junto con MODO_PRUEBAS=True (bot de pruebas). Aquí solo
#     se agrega una COPIA a logs/simulacion_*.log.
#   - Se CORTA todo contacto con el backend real de Pochonance/Railway:
#       · el bridge de movimientos (ver pochonance_bridge.py), y
#       · el activador (no se arranca, ver iniciar.py), que es quien pedía
#         abrir cuentas a Railway. En simulación las cuentas se abren A MANO
#         desde el menú del bot ("🚀 Iniciar monitoreo").
#   - inyeccion.py mete movimientos artificiales (carpeta inyeccion_entrada/)
#     que el loop de cada cuenta procesa como si Bancolombia los mostrara.
#
# SEGURIDAD: el modo simulación EXIGE MODO_PRUEBAS=True (bots de prueba).
# iniciar.py aborta si se enciende la simulación sin MODO_PRUEBAS, para
# que jamás se envíe algo a los bots reales por error.
#
# Encender:  variable de entorno  $env:MOVS_SIMULACION="1"   (PowerShell)
#            o  MODO_SIMULACION = True  en config.py
# =====================================================================
import os
import json
import glob
import threading
import uuid as _uuid
from datetime import datetime


def _leer_flag() -> bool:
    env = os.environ.get("MOVS_SIMULACION", "").strip().lower()
    if env in ("1", "true", "si", "sí", "on", "yes"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        from config import MODO_SIMULACION
        return bool(MODO_SIMULACION)
    except Exception:
        return False

_ACTIVO = _leer_flag()

def activo() -> bool:
    return _ACTIVO

def pruebas_activo() -> bool:
    """True si MODO_PRUEBAS=True en config (bots de prueba + backend local)."""
    try:
        from config import MODO_PRUEBAS
        return bool(MODO_PRUEBAS)
    except Exception:
        return False


# ── Carpetas ────────────────────────────────────────────────────────
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inyeccion_entrada")
_DIR_LOGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
ARCHIVO_ACTIVAS = os.path.join(_DIR, "_activas.txt")
_lock = threading.Lock()

def _asegurar_dirs():
    for d in (_DIR, _DIR_LOGS):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


# ── Copia a archivo de cada notificación (además del bot de prueba) ──
def log_archivo(texto: str) -> None:
    if not _ACTIVO:
        return
    _asegurar_dirs()
    try:
        hora = datetime.now().strftime("%H:%M:%S")
        ruta = os.path.join(_DIR_LOGS, f"simulacion_{datetime.now():%Y-%m-%d}.log")
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"[{hora}] " + " ".join((texto or "").split()) + "\n")
    except Exception:
        pass


# ── Inyección de movimientos artificiales ───────────────────────────
def publicar_activas(nombres) -> None:
    if not _ACTIVO:
        return
    _asegurar_dirs()
    try:
        with _lock, open(ARCHIVO_ACTIVAS, "w", encoding="utf-8") as f:
            f.write("\n".join(nombres))
    except Exception:
        pass


def inyectar(cuenta: str, txs: list) -> str:
    """inyeccion.py deja un archivo con txs para una cuenta (escritura atómica)."""
    _asegurar_dirs()
    destino = os.path.join(_DIR, f"{_uuid.uuid4().hex}.json")
    tmp = destino + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cuenta": cuenta, "txs": txs}, f, ensure_ascii=False)
    os.replace(tmp, destino)
    return destino


def tomar_inyectados(cuenta: str) -> list:
    """Cada cuenta, cada ciclo (solo en simulación): recoge y consume lo suyo."""
    if not _ACTIVO:
        return []
    recogidos: list = []
    with _lock:
        for ruta in glob.glob(os.path.join(_DIR, "*.json")):
            try:
                with open(ruta, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            c = (data or {}).get("cuenta", "")
            if c == cuenta or c == "*":
                recogidos.extend((data or {}).get("txs", []))
                try:
                    os.remove(ruta)
                except Exception:
                    pass
    return recogidos


# ── Cuentas que la simulación abre sola al arrancar ─────────────────
# Se abren sin menú y sin Pochonance. Editable en cuentas_simulacion.txt
# (una cuenta por línea; líneas con # se ignoran).
_CUENTAS_DEFAULT = [
    "Cuenta de prueba 1",
    "Cuenta de prueba 2",
    "Cuenta de prueba 3",
]
ARCHIVO_CUENTAS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cuentas_simulacion.txt")

def cuentas_a_abrir() -> list:
    """Lista de cuentas a abrir en simulación. Si el archivo no existe, lo
    crea con las de ejemplo para que lo puedas editar."""
    if not os.path.exists(ARCHIVO_CUENTAS):
        try:
            with open(ARCHIVO_CUENTAS, "w", encoding="utf-8") as f:
                f.write("# Una cuenta por línea. Las que empiezan con # se ignoran.\n")
                f.write("\n".join(_CUENTAS_DEFAULT) + "\n")
        except Exception:
            return list(_CUENTAS_DEFAULT)
    try:
        with open(ARCHIVO_CUENTAS, encoding="utf-8") as f:
            return [l.strip() for l in f
                    if l.strip() and not l.strip().startswith("#")]
    except Exception:
        return list(_CUENTAS_DEFAULT)


def banner():
    """Letrero grande al arrancar. Inconfundible."""
    if not _ACTIVO:
        return
    pruebas = pruebas_activo()
    print("")
    print("████████████████████████████████████████████████████████████")
    print("█  ⚠️  MODO SIMULACIÓN ACTIVO                               █")
    print("█  Pochonance/Railway CORTADO (bridge + activador).        █")
    print("█  Abre las cuentas A MANO desde el menú del bot.          █")
    if pruebas:
        print("█  Notificaciones → bot de PRUEBA + logs/simulacion_*.log  █")
    else:
        print("█  ⛔ FALTA MODO_PRUEBAS=True → el sistema NO arrancará.    █")
    print("████████████████████████████████████████████████████████████")
    print("")
