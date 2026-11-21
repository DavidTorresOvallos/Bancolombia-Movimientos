# -*- coding: utf-8 -*-
# =====================================================================
# POCHONANCE_LECTURAS.PY — Movimientos comparte lo que lee del banco
#
# Una cuenta abierta en Movimientos no puede abrirse otra vez en Conciliación: Bancolombia solo deja una sesión y la
# segunda le cierra la primera. Para poder verificar los retiros de esas cuentas, Movimientos le pasa a Pochonance lo
# que YA lee de cada una (el saldo y las últimas transacciones de la API del banco) y Conciliación usa eso en vez de
# entrar al banco. Movimientos no hace nada extra: no abre páginas ni consulta al banco, solo reenvía lo que cada
# cuenta tiene en memoria, cada tantos segundos. La comparación y el aviso los hace Conciliación.
#
# Solo se comparte una lectura RECIENTE (la cuenta leyó el banco hace poco). Si la cuenta se traba o se cierra, deja
# de enviarse y Pochonance entiende que ya no está abierta aquí (la lectura vence a los 3 minutos).
#
# Se apaga con COMPARTIR_LECTURAS = False en config.py.
# =====================================================================
import threading
import time

import requests

import config as _config

COMPARTIR_LECTURAS = bool(getattr(_config, "COMPARTIR_LECTURAS", True))
INTERVALO_LECTURAS_SEGUNDOS = float(getattr(_config, "INTERVALO_LECTURAS_SEGUNDOS", 30))
# Una lectura de más de estos segundos ya no se comparte (la cuenta se quedó sin leer el banco).
MAX_EDAD_LECTURA_SEGUNDOS = float(getattr(_config, "MAX_EDAD_LECTURA_SEGUNDOS", 90))


def _parsear_pesos(texto):
    from notificar import _parsear_pesos as parsear
    return parsear(texto)


def armar_lectura(monitor, nodo: str, ahora: float | None = None):
    """El cuerpo del envío para esta cuenta, o None si no hay nada reciente que compartir."""
    ahora = time.time() if ahora is None else ahora
    ts = getattr(monitor, "_ultima_lectura_ts", 0.0) or 0.0
    snapshot = list(getattr(monitor, "_last_snapshot", None) or [])
    if not ts or not snapshot:
        return None   # todavía no leyó el banco
    edad = ahora - ts
    if edad > MAX_EDAD_LECTURA_SEGUNDOS:
        return None   # lectura vieja: no se comparte como si fuera de ahora
    saldo = _parsear_pesos(getattr(monitor, "saldo_actual", None) or "")
    if saldo is None:
        return None
    return {
        "cuenta": monitor.nombre,
        "nodo": nodo,
        "saldo": float(saldo),
        "leidaHaceSegundos": int(max(0, edad)),
        "movimientos": snapshot,
    }


def motivo_sin_lectura(monitor, ahora: float | None = None) -> str:
    """Por qué armar_lectura no devuelve nada para esta cuenta (para dejarlo en el log)."""
    ahora = time.time() if ahora is None else ahora
    ts = getattr(monitor, "_ultima_lectura_ts", 0.0) or 0.0
    if not ts:
        return "todavía no ha leído el banco"
    if not (getattr(monitor, "_last_snapshot", None) or []):
        return "no tiene movimientos leídos"
    if ahora - ts > MAX_EDAD_LECTURA_SEGUNDOS:
        return f"la última lectura es de hace {int(ahora - ts)}s (límite {MAX_EDAD_LECTURA_SEGUNDOS:.0f}s)"
    return f"no se pudo leer el saldo (saldo_actual={getattr(monitor, 'saldo_actual', None)!r})"


# Cada cuenta se anuncia una vez al empezar a compartirse y cuando deja de hacerlo; el motivo se repite como mucho cada
# REPETIR_MOTIVO_SEGUNDOS para no llenar el log.
REPETIR_MOTIVO_SEGUNDOS = 300
_estado_log: dict = {}


def _anunciar(nombre: str, compartiendo: bool, detalle: str, ahora: float):
    previo = _estado_log.get(nombre)
    if compartiendo:
        if not previo or not previo["compartiendo"]:
            print(f"[Lecturas] ✅ {nombre}: ya se comparte con Pochonance ({detalle}).")
        _estado_log[nombre] = {"compartiendo": True, "ultimo": ahora}
        return
    if not previo or previo["compartiendo"] or ahora - previo["ultimo"] >= REPETIR_MOTIVO_SEGUNDOS:
        print(f"[Lecturas] ⏸️ {nombre}: NO se comparte — {detalle}.")
        _estado_log[nombre] = {"compartiendo": False, "ultimo": ahora}


def compartir_una_vez(sesiones_activas: dict, nodo: str, enviar) -> int:
    """Envía la lectura de cada cuenta viva. 'enviar(cuerpo)' hace el POST (se inyecta para poder probar).
    Devuelve cuántas se enviaron. Un fallo con una cuenta no afecta a las demás."""
    enviadas = 0
    for _nombre, monitor in list(sesiones_activas.items()):
        try:
            if not monitor.is_alive():
                continue
            cuerpo = armar_lectura(monitor, nodo)
            if cuerpo is None:
                _anunciar(_nombre, False, motivo_sin_lectura(monitor), time.time())
                continue
            if enviar(cuerpo) is False:
                continue   # el motivo ya lo dijo 'enviar'
            _anunciar(_nombre, True, f"saldo {cuerpo['saldo']:,.0f}, {len(cuerpo['movimientos'])} movimientos",
                      time.time())
            enviadas += 1
        except Exception as e:
            print(f"⚠️ [Lecturas] No se pudo compartir '{_nombre}': {e}")
    return enviadas


def iniciar_compartir_lecturas(sesiones_activas: dict, url_base: str, api_key: str, headers_extra: dict, nodo: str):
    """Arranca el hilo. Devuelve el hilo, o None si está apagado. Nunca afecta al monitoreo: todo fallo se ignora."""
    if not COMPARTIR_LECTURAS:
        print("[Lecturas] Apagado (COMPARTIR_LECTURAS = False en config.py).")
        return None
    url = f"{url_base}/movimientos/lectura"
    headers = {"X-Bot-Api-Key": api_key, **(headers_extra or {})}
    estado = {"fallos": 0}

    def enviar(cuerpo):
        try:
            resp = requests.post(url, json=cuerpo, headers=headers, timeout=10)
            if resp.status_code >= 300:
                if estado["fallos"] == 0:
                    print(f"⚠️ [Lecturas] /movimientos/lectura respondió {resp.status_code}: {resp.text[:160]}")
                estado["fallos"] += 1
                return False
            estado["fallos"] = 0
            try:
                guardada = (resp.json() or {}).get("ok", True)
            except ValueError:
                guardada = True   # respondió 2xx sin JSON: se da por recibido
            if not guardada:
                print(f"⚠️ [Lecturas] Pochonance no guardó la lectura de '{cuerpo.get('cuenta')}' "
                      f"(¿el nombre no es único o no existe en Pochonance?).")
                return False
            return True
        except requests.RequestException as e:
            if estado["fallos"] == 0:
                print(f"⚠️ [Lecturas] Sin conexión con Pochonance ({e}); reintento en silencio.")
            estado["fallos"] += 1
            return False

    def bucle():
        print(f"[Lecturas] 📤 Compartiendo con Pochonance la lectura de las cuentas abiertas, cada "
              f"{INTERVALO_LECTURAS_SEGUNDOS:.0f}s (nodo {nodo}).")
        while True:
            time.sleep(INTERVALO_LECTURAS_SEGUNDOS)
            try:
                compartir_una_vez(sesiones_activas, nodo, enviar)
            except Exception as e:
                print(f"⚠️ [Lecturas] {e}")

    hilo = threading.Thread(target=bucle, name="compartir-lecturas", daemon=True)
    hilo.start()
    return hilo
