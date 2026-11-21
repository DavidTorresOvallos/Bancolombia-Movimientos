# -*- coding: utf-8 -*-
# =====================================================================
# POCHONANCE_ACTIVADOR.PY
# Responsabilidad: escuchar cuando Pochonance activa una cuenta en
# "Cuentas P2P" y arrancar su monitoreo automáticamente — SIN pasar por
# Telegram, reusando exactamente el mismo mecanismo que ya usa el botón
# "🚀 Iniciar monitoreo" del menú (ver mon_comenzar en iniciar.py):
# MonitorThread + el registro compartido _sesiones_activas. Así nunca
# hay dos monitores corriendo para la misma cuenta sin importar si se
# arrancó desde Telegram o desde Pochonance.
#
# UBICACIÓN: vive en Movimientos/Pochonance/ junto con pochonance_bridge.py
# (el otro script de conexión con la plataforma Pochonance).
#
# NO ES UNA COLA NUEVA: el "activar cuenta en Cuentas P2P" YA encola una
# ConciliacionSolicitud del lado de Pochonance desde hace rato (ver
# AccountCopController.toggleActivaParaP2P → ConciliacionBancariaServiceImpl
# .solicitarConciliacion), pensada originalmente para que un bot la
# "revisara". Este archivo reutiliza ESA misma cola / mismo endpoint
# (GET /conciliacion/pendiente) en vez de inventar una segunda cola — el
# bot de conciliación bancaria (Automatizacion Bancolombia/conciliacion_
# bancaria.py) ya tiene esa respuesta DESACTIVADA a propósito para no
# competir por la misma solicitud (mismo motivo por el que existió, y ya
# se eliminó, Pochonance/pochonance.py — ver control de versiones).
#
# RELACIÓN con pochonance_bridge.py — dos canales, cada uno con su rol:
#   - Este archivo SOLO consume el disparador (qué cuenta activar) y
#     arranca el hilo de monitoreo. No le reporta nada a Pochonance por sí
#     mismo, salvo cuando ni siquiera puede intentarlo (sin credenciales en
#     Bitwarden) o si algo inesperado revienta — ahí sí usa
#     bridge_error_login (mismo canal de abajo) para dejar el aviso.
#   - pochonance_bridge.py reporta, ya con el navegador abierto y de verdad
#     habiendo intentado entrar a Bancolombia, "conexion_exitosa" (con saldo
#     y movimientos — la prueba real de que la cuenta sirve) o "error_login"
#     (con el motivo, ej. bloqueo detectado). Ver sesiones.py → MonitorThread,
#     que es quien dispara esto automáticamente al terminar su primer intento.
# Ya no existe un script aparte que "compruebe" si la cuenta está activa
# (Pochonance/pochonance.py, eliminado) — la propia primera conexión de
# MonitorThread ES la comprobación.
#
# IMPORTANTE (para que Pochonance nunca vuelva a bloquear cuentas en cadena
# por un fallo del BOT, no del banco — ver incidente 15/08/2026): el
# resultado de "conexion_exitosa"/"error_login" JAMÁS bloquea la cuenta
# automáticamente del lado de Pochonance — eso está fijado en
# ConciliacionBancariaServiceImpl.registrarResultadoCuenta (backend). Este
# archivo no tiene que preocuparse por eso, pero si algún día se toca esa
# regla, hay que tocarla ahí, no acá.
#
# QUIETO A PROPÓSITO: iniciar.py solo necesita estas líneas para
# integrar este archivo (ya agregadas, ver el bloque MAIN al final):
#
#   from Pochonance.pochonance_activador import iniciar_polling_activacion
#   ...
#   iniciar_polling_activacion(_sesiones_activas, _actualizar_conteo)
#
# ── DETENCIÓN (agosto 2026) ──────────────────────────────────────────
# Mismo archivo también escucha la cola de DETENCIÓN: cuando una cuenta
# deja de estar seleccionada en P2P (se llenó de cupo, se desactivó a
# mano, el bot no pudo confirmarla, etc. — ver CuentaP2PSyncService en
# el backend), hay que parar SU sesión puntual sin tocar las demás. A
# diferencia de la activación, para esto SÍ hizo falta una cola nueva
# (GET/POST /movimientos/detencion/*, MovimientosBridgeController) —
# no existía ningún mecanismo previo para que Pochonance le pidiera al
# bot que parara una cuenta en concreto. El PARAR en sí reusa el mismo
# MonitorThread.stop() ya probado (ver el botón "🛑 Detener todo" en
# iniciar.py): señaliza en un hilo aparte, nunca toca Playwright desde
# fuera de su propio hilo.
#
# CONTRATO con el backend:
#
#   GET  /conciliacion/pendiente            (YA EXISTÍA, ConciliacionBancariaController)
#        Header: X-Bot-Api-Key: <app.conciliacion.api-key>
#        → 204 si no hay ninguna cuenta esperando ser activada
#        → 200 {"cuenta": "Nombre Exacto Como Está En Bitwarden"} (y la
#          marca como consumida del lado del backend)
#
#   POST /movimientos/evento                (YA EXISTÍA, MovimientosBridgeController,
#        Header: X-Bot-Api-Key: <app.cuentasp2p.bridge-api-key>   ver pochonance_bridge.py)
#        evento="error_login" — usado acá solo para "sin credenciales" o
#        errores inesperados al intentar arrancar (ver _arrancar_cuenta).
#
# OJO: /conciliacion/pendiente no es atómico del lado del backend (sin
# SELECT ... FOR UPDATE) — en teoría dos polls muy seguidos podrían recibir
# la MISMA cuenta dos veces. Es inofensivo acá: _arrancar_cuenta() ya es
# idempotente (si la cuenta ya está activa, no hace nada).
#
# USO: no se ejecuta solo — lo arranca iniciar.py como hilo de fondo
# (daemon) al final de su arranque.
# =====================================================================
import os
import sys
import threading
import time

# Bootstrap: Movimientos/ (el padre de esta carpeta Pochonance/) tiene que
# estar en sys.path para poder importar sus módulos (notificar, login,
# sesiones) sin importar desde dónde se ejecute/importe este módulo.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from notificar import _enviar, OWNER_CHAT_ID
from login import obtener_credenciales
from sesiones import MonitorThread, COOLDOWN_CUENTAS as _COOLDOWN_CUENTAS
from Pochonance.pochonance_bridge import bridge_error_login
import nodos

# ── Config API de Pochonance ───────────────────────────────────────
# INTERRUPTOR DE SEGURIDAD: en False apunta al backend LOCAL
# (application-dev.properties), nunca a producción — así correr iniciar.py
# en una máquina de prueba jamás arranca monitoreo de cuentas reales por
# accidente. Puesto en True A PROPÓSITO (31/08/2026) para la prueba en
# producción — recuerda volver a False si vuelves a probar en local
# después de esto.
USAR_PRODUCCION = True

if USAR_PRODUCCION:
    _API_BASE = "https://backend-binance-resumen-production.up.railway.app"
    # app.conciliacion.api-key en application-prod.properties.
    _API_KEY_CONCILIACION = ""
    # app.cuentasp2p.bridge-api-key en application-prod.properties — YA
    # configurada (se agregó el 29/08/2026, ver commit b75b839), a
    # diferencia de lo que decía esta nota antes. /movimientos/detencion/*
    # y /movimientos/evento ya no dan 503 en producción por esto.
    _API_KEY_CUENTASP2P = ""
else:
    # Backend corriendo en tu máquina con:
    #   mvn spring-boot:run "-Dspring-boot.run.profiles=dev"
    _API_BASE = "http://localhost:8080"
    # app.conciliacion.api-key en application-dev.properties.
    _API_KEY_CONCILIACION = ""
    # app.cuentasp2p.bridge-api-key en application-dev.properties.
    _API_KEY_CUENTASP2P = ""

# Las claves salen de config.py (gitignored, local de cada nodo). Aquí NO se
# escribe ninguna clave: el repo no debe contener secretos.
try:
    import config as _config
except Exception:
    _config = None
_API_KEY_CONCILIACION = getattr(_config, "POCHONANCE_API_KEY_CONCILIACION", "") or _API_KEY_CONCILIACION
_API_KEY_CUENTASP2P   = getattr(_config, "POCHONANCE_API_KEY", "") or _API_KEY_CUENTASP2P

# Nodo de este PC. "A" = comportamiento de siempre: NO se envía ningún
# parámetro nuevo al backend. Un nodo distinto de "A" manda `nodo` en las
# colas y en el latido, y no arranca hasta que BACKEND_SOPORTA_NODOS sea True
# (un backend viejo ignoraría `nodo` y este PC le robaría las órdenes al A).
NODO_ID = str(getattr(_config, "NODO_ID", "A") or "A").strip().upper()
_PARAMS_NODO = {"nodo": NODO_ID} if NODO_ID != "A" else {}

_URL_PENDIENTE  = f"{_API_BASE}/conciliacion/pendiente"
_URL_DETENCION_PENDIENTE = f"{_API_BASE}/movimientos/detencion/pendiente"
_URL_DETENCION_RESULTADO = f"{_API_BASE}/movimientos/detencion/resultado"
_URL_HEARTBEAT = f"{_API_BASE}/movimientos/heartbeat"
_HEADERS_EXTRA  = {"ngrok-skip-browser-warning": "true"}

# Cada cuántos segundos se pregunta si hay algo pendiente cuando no hay
# nada por hacer. Si hay algo pendiente, se sigue de una vez (ver _loop_polling).
INTERVALO_POLLING_SEGUNDOS = 5

# Cada cuánto se le reporta al backend qué cuentas están corriendo AHORA
# MISMO (ver _loop_heartbeat / MovimientosHeartbeatServiceImpl en el
# backend). Es solo una red de seguridad para el caso raro en que una orden
# de activar/detener se pierda silenciosamente — no hace falta que sea
# frecuente.
INTERVALO_HEARTBEAT_SEGUNDOS = 10  # bajado de 120s (04/09/2026): Pochonance ahora
# detecta caídas de conexión con un umbral de 30s (ver
# MovimientosConectividadMonitor.java) — con heartbeats cada 120s, cualquier
# ciclo normal ya dispararía una falsa alarma de "conexión perdida".

# Chat que recibe el aviso de "cuenta activada" (además de lo que ya
# notifique el propio MonitorThread cuando detecte transacciones). Se
# fija una sola vez al arrancar, vía iniciar_polling_activacion().
_CHAT_ID_NOTIFICACION = OWNER_CHAT_ID


# Sentinel: distingue "nada pendiente (204)" de "error de red"
_SIN_BACKEND = object()

# Cuentas cuyo MonitorThread viejo está en proceso de apagarse (stop() ya
# se llamó, pero el hilo todavía no terminó de cerrar su sesión de
# Playwright/Bancolombia) — ver _detener_cuenta / _arrancar_cuenta
# (05/09/2026). Sin esto, una reactivación muy seguida a una detención
# (típico del vaivén de "cupo agotado" de AsignacionAutomaticaService)
# podía arrancar un MonitorThread nuevo mientras el viejo de la MISMA
# cuenta todavía seguía vivo unos segundos más — dos sesiones abiertas a
# la vez en la misma cuenta de Bancolombia, con síntomas como el aviso de
# "conexión exitosa" duplicado que reportó el equipo.
_monitores_apagandose = {}

def _parsear_orden(data):
    """Orden de Pochonance → {"cuenta", "id", "nodo"} (id y nodo pueden venir None
    si el backend aún no los manda: en ese caso nodos.py rechaza la orden)."""
    data = data or {}
    cuenta = data.get("cuenta")
    if not cuenta:
        return None
    return {"cuenta": cuenta, "id": data.get("id"), "nodo": data.get("nodo")}


def _get_pendiente():
    """Devuelve:
      - dict  → {"cuenta", "id", "nodo"} de la cuenta pendiente (ver _parsear_orden)
      - None  → 204, no hay nada pendiente (backend OK)
      - _SIN_BACKEND → error de conexión/red (backend no disponible)
    """
    try:
        resp = requests.get(
            _URL_PENDIENTE,
            headers={"X-Bot-Api-Key": _API_KEY_CONCILIACION, **_HEADERS_EXTRA},
            params=_PARAMS_NODO,
            timeout=10,
        )
        if resp.status_code == 204:
            return None
        if resp.status_code == 200:
            return _parsear_orden(resp.json())
        print(f"⚠️ [Activación] /conciliacion/pendiente respondió "
              f"{resp.status_code}: {resp.text[:200]}")
        return None
    except Exception:
        return _SIN_BACKEND


def _arrancar_cuenta(nombre: str, sesiones_activas: dict, actualizar_conteo_fn):
    """Arranca el monitoreo de UNA cuenta, replicando exactamente lo que
    hace el botón '🚀 Iniciar monitoreo' del menú de Telegram (ver
    mon_comenzar en iniciar.py). El resultado real (conexión OK o no) lo
    reporta MonitorThread solo, vía bridge_conexion_exitosa/bridge_error_login
    (ver sesiones.py) — acá solo se cubre el caso en que ni siquiera se pudo
    intentar (sin credenciales)."""

    # Ya activa: no hay nada que hacer — Pochonance ya sabe que esta cuenta
    # sirve desde que se activó la primera vez (bridge_conexion_exitosa).
    if nombre in sesiones_activas and sesiones_activas[nombre].is_alive():
        print(f"[Activación] ℹ️ {nombre} ya estaba activa — se deja corriendo sin cambios.")
        return

    # Se detuvo hace poco porque Bancolombia no le mostró el menú: no se reabre de inmediato (el latido
    # la vuelve a pedir cada 10 s y repetiría el bucle). Se aplica solo a órdenes de Pochonance.
    _resto = _COOLDOWN_CUENTAS.get(nombre, 0) - time.time()
    if _resto > 0:
        print(f"[Activación] ⏸️ {nombre} se detuvo hace poco (Bancolombia no mostró su menú) — "
              f"no se reabre; faltan ~{_resto / 60:.0f} min")
        return

    usuario, contrasena = obtener_credenciales(nombre)
    if not usuario:
        # A diferencia del incidente 15/08/2026 (donde esto se reportaba como
        # si fuera un resultado del banco y terminaba bloqueando cuentas en
        # cadena), acá "error_login" NUNCA bloquea nada — ver
        # ConciliacionBancariaServiceImpl.registrarResultadoCuenta. Solo deja
        # el aviso "no disponible: Sin credenciales en Bitwarden" para revisión manual.
        print(f"[Activación] ❌ Sin credenciales en Bitwarden para '{nombre}'.")
        bridge_error_login(nombre, "Sin credenciales en Bitwarden en esta máquina")
        return

    # Si quedó una entrada vieja de un monitor ya detenido, se limpia antes
    # de crear uno nuevo (mismo patrón que mon_comenzar).
    if nombre in sesiones_activas:
        try:
            sesiones_activas[nombre].stop()
        except Exception:
            pass

    # Si esta cuenta se acaba de detener (p.ej. por "cupo agotado" en
    # AsignacionAutomaticaService) y esa reactivación llega muy seguida,
    # el MonitorThread viejo puede seguir vivo todavía cerrando su sesión
    # de Bancolombia — esperamos un poco para no abrir una segunda sesión
    # de la MISMA cuenta en paralelo (05/09/2026, ver _monitores_apagandose).
    viejo = _monitores_apagandose.get(nombre)
    if viejo is not None:
        espera_max = 10  # segundos — no bloquear la cola de activaciones para siempre
        inicio_espera = time.time()
        while viejo.is_alive() and (time.time() - inicio_espera) < espera_max:
            time.sleep(0.5)
        if viejo.is_alive():
            print(f"[Activación] ⚠️ '{nombre}' seguía cerrando su sesión anterior tras "
                  f"{espera_max}s — se abre la nueva de todas formas.")

    monitor = MonitorThread(nombre, usuario, contrasena, _CHAT_ID_NOTIFICACION)
    monitor.start()
    sesiones_activas[nombre] = monitor
    actualizar_conteo_fn()

    print(f"[Activación] ✅ {nombre} — monitoreo iniciado (resultado real lo reporta MonitorThread).")

    # A PROPÓSITO (agosto 2026, a pedido del equipo): NO se manda aviso al chat
    # de Telegram del bot de Movimientos (Saldo) — eso lo ensuciaba con un
    # mensaje por cada cuenta que la selección automática de P2P activa o
    # rota durante el día. El chat de Movimientos solo debe recibir
    # notificaciones reales de movimientos/saldo, como siempre. El aviso de
    # "cuenta activada/detenida desde Pochonance" vive en el bot "Cuentas
    # P2P" (canal aparte, ver MovimientosBridgeServiceImpl en el backend),
    # que si el bot logra conectarse reportará "conexion_exitosa" con el
    # saldo real — esa sí es la confirmación útil.


def _loop_polling(sesiones_activas: dict, actualizar_conteo_fn):
    print(f"[Activación] 🔌 Escuchando activaciones de Pochonance ({_API_BASE})...")
    _fallos_red = 0   # cuántos polls consecutivos fallaron por red
    while True:
        resultado = _get_pendiente()

        # ── Error de red / backend no disponible ──────────────────────
        if resultado is _SIN_BACKEND:
            if _fallos_red == 0:
                # Primera falla: avisar una sola vez
                print(f"⚠️ [Activación] Backend no disponible en {_API_BASE} "
                      f"— reintentando en silencio cada {INTERVALO_POLLING_SEGUNDOS}s...")
            _fallos_red += 1
            time.sleep(INTERVALO_POLLING_SEGUNDOS)
            continue

        # ── Backend respondió (204 o nombre) ────────────────────────
        if _fallos_red > 0:
            print(f"[Activación] ✅ Backend disponible de nuevo ({_API_BASE}).")
            _fallos_red = 0

        if not resultado:
            # 204 — nada pendiente, esperar y volver
            time.sleep(INTERVALO_POLLING_SEGUNDOS)
            continue

        nombre = resultado["cuenta"]
        id_pochonance = resultado.get("id")
        print(f"[Activación] 🔔 Solicitud pendiente: {nombre} (ID {id_pochonance})")
        # Esta orden pasó por la cola de Pochonance, que ya la consumió: si NO es de
        # este nodo no se abre nada y se avisa (nodos.py), nunca en silencio.
        ok, motivo = nodos.decidir_pochonance(nombre, id_pochonance, resultado.get("nodo"))
        if not ok:
            nodos.avisar_rechazo("activación", nombre, id_pochonance, motivo)
            continue
        try:
            _arrancar_cuenta(nombre, sesiones_activas, actualizar_conteo_fn)
        except Exception as e:
            print(f"[Activación] ❌ Error inesperado arrancando '{nombre}': {e}")
            bridge_error_login(nombre, f"Error inesperado al arrancar: {e}")
        # Sin pausa a propósito: si hay más de una solicitud pendiente,
        # se procesan una tras otra sin esperar el intervalo completo.


# ══════════════════════════════════════════════════════════════════
# DETENCIÓN — polling de /movimientos/detencion/pendiente
# ══════════════════════════════════════════════════════════════════

def _get_detencion_pendiente():
    """Igual que _get_pendiente() pero contra la cola de detención."""
    try:
        resp = requests.get(
            _URL_DETENCION_PENDIENTE,
            headers={"X-Bot-Api-Key": _API_KEY_CUENTASP2P, **_HEADERS_EXTRA},
            params=_PARAMS_NODO,
            timeout=10,
        )
        if resp.status_code == 204:
            return None
        if resp.status_code == 200:
            return _parsear_orden(resp.json())
        print(f"⚠️ [Detención] /movimientos/detencion/pendiente respondió "
              f"{resp.status_code}: {resp.text[:200]}")
        return None
    except Exception:
        return _SIN_BACKEND


def _reportar_detencion_resultado(nombre: str, detenida: bool, error: str = None):
    try:
        requests.post(
            _URL_DETENCION_RESULTADO,
            json={"cuenta": nombre, "detenida": detenida, "error": error},
            headers={"X-Bot-Api-Key": _API_KEY_CUENTASP2P, **_HEADERS_EXTRA},
            timeout=10,
        )
    except Exception as e:
        print(f"⚠️ [Detención] No se pudo reportar resultado de '{nombre}': {e}")


def _detener_cuenta(nombre: str, sesiones_activas: dict, actualizar_conteo_fn):
    """Para el MonitorThread de UNA cuenta puntual, replicando exactamente el
    patrón seguro que ya usa el botón '🛑 Detener todo' del menú de Telegram
    (ver accion == 'detener' en iniciar.py): stop() en un hilo aparte (nunca
    toca Playwright desde este hilo), y solo DESPUÉS se saca del diccionario
    compartido — así el menú de Telegram y esta cola nunca pisan sesiones del
    otro lado por accidente."""
    monitor = sesiones_activas.get(nombre)
    if not monitor:
        print(f"[Detención] ℹ️ {nombre} no estaba en monitoreo — nada que parar.")
        _reportar_detencion_resultado(nombre, True)  # ya estaba en el estado deseado
        return

    def _parar(m=monitor):
        try:
            # Detención suave: termina el ciclo en curso (≤20s) para notificar
            # lo que ya vio — ej. el retiro que causó el "cupo agotado".
            m.detener_suave() if hasattr(m, "detener_suave") else m.stop()
            # Esperar a que el hilo realmente termine de cerrar su sesión de
            # Bancolombia (no solo señalizar) — así, si llega una reactivación
            # rápida para la misma cuenta, _arrancar_cuenta puede saber que
            # todavía sigue vivo y esperar, en vez de abrir una segunda sesión
            # en paralelo (05/09/2026, ver _monitores_apagandose).
            m.join(timeout=30)
            if m.is_alive():
                print(f"[Detención] ⚠️ '{nombre}' no terminó de cerrar tras 30s "
                      f"— se libera igual (puede quedar una sesión de Bancolombia sin cerrar del todo).")
        except Exception as e:
            print(f"[Detención] ⚠️ Error al parar '{nombre}': {e}")
        finally:
            _monitores_apagandose.pop(nombre, None)
    _monitores_apagandose[nombre] = monitor
    threading.Thread(target=_parar, daemon=True).start()

    sesiones_activas.pop(nombre, None)
    actualizar_conteo_fn()
    _borrar_cache_cuenta_si_existe(nombre)

    print(f"[Detención] 🛑 {nombre} — monitoreo detenido (cupo agotado / desactivada de P2P).")
    # Sin aviso al chat de Movimientos — ver comentario igual en _arrancar_cuenta.
    _reportar_detencion_resultado(nombre, True)


def _borrar_cache_cuenta_si_existe(nombre: str):
    """Best-effort: reusa _borrar_cache_cuenta de iniciar.py si está disponible
    (mismo criterio que usa 'Detener todo' / 'Detener cuenta' por Telegram),
    sin fallar si por algún motivo no se puede importar."""
    try:
        # Tomarla del iniciar.py que YA está corriendo (__main__). Un
        # `from iniciar import …` lo cargaría como módulo nuevo y volvería a
        # ejecutar todo su código (handlers de Telegram, print con hora ×2…).
        _main = sys.modules.get("__main__")
        _borrar = getattr(_main, "_borrar_cache_cuenta", None)
        if _borrar:
            _borrar(nombre)
    except Exception:
        pass


def _loop_polling_detencion(sesiones_activas: dict, actualizar_conteo_fn):
    print(f"[Detención] 🔌 Escuchando detenciones de Pochonance ({_API_BASE})...")
    _fallos_red = 0
    while True:
        resultado = _get_detencion_pendiente()

        if resultado is _SIN_BACKEND:
            if _fallos_red == 0:
                print(f"⚠️ [Detención] Backend no disponible en {_API_BASE} "
                      f"— reintentando en silencio cada {INTERVALO_POLLING_SEGUNDOS}s...")
            _fallos_red += 1
            time.sleep(INTERVALO_POLLING_SEGUNDOS)
            continue

        if _fallos_red > 0:
            print(f"[Detención] ✅ Backend disponible de nuevo ({_API_BASE}).")
            _fallos_red = 0

        if not resultado:
            time.sleep(INTERVALO_POLLING_SEGUNDOS)
            continue

        nombre = resultado["cuenta"]
        id_pochonance = resultado.get("id")
        print(f"[Detención] 🔔 Solicitud pendiente: {nombre} (ID {id_pochonance})")
        # Si no es de este nodo no se toca nada ni se reporta "detenida" (sería mentira).
        ok, motivo = nodos.decidir_pochonance(nombre, id_pochonance, resultado.get("nodo"))
        if not ok:
            nodos.avisar_rechazo("detención", nombre, id_pochonance, motivo)
            continue
        try:
            _detener_cuenta(nombre, sesiones_activas, actualizar_conteo_fn)
        except Exception as e:
            print(f"[Detención] ❌ Error inesperado deteniendo '{nombre}': {e}")
            _reportar_detencion_resultado(nombre, False, str(e))


def _reportar_heartbeat(sesiones_activas: dict):
    """Le dice al backend qué cuentas tiene corriendo AHORA MISMO (sesiones
    de MonitorThread vivas), para que Pochonance pueda detectar y corregir
    activaciones/detenciones que se hayan perdido silenciosamente (ver
    MovimientosHeartbeatServiceImpl en el backend, y el incidente
    "un titular" del 03/09/2026 que motivó esto). Best-effort a
    propósito: un fallo acá nunca debe afectar el monitoreo real, solo se
    reintenta en el próximo ciclo."""
    try:
        cuentas_activas = [
            nombre for nombre, monitor in list(sesiones_activas.items())
            if monitor.is_alive()
        ]
        payload = {"cuentasActivas": cuentas_activas, **_PARAMS_NODO}
        resp = requests.post(
            _URL_HEARTBEAT,
            json=payload,
            headers={"X-Bot-Api-Key": _API_KEY_CUENTASP2P, **_HEADERS_EXTRA},
            timeout=10,
        )
        if resp.status_code >= 300:
            print(f"⚠️ [Heartbeat] /movimientos/heartbeat respondió "
                  f"{resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"⚠️ [Heartbeat] No se pudo reportar estado al backend: {e}")


def _loop_heartbeat(sesiones_activas: dict):
    print(f"[Heartbeat] 🔌 Reportando estado a Pochonance cada {INTERVALO_HEARTBEAT_SEGUNDOS}s...")
    while True:
        time.sleep(INTERVALO_HEARTBEAT_SEGUNDOS)
        _reportar_heartbeat(sesiones_activas)


def iniciar_polling_activacion(sesiones_activas: dict, actualizar_conteo_fn,
                                chat_id_notificacion: int = None) -> threading.Thread:
    """Único punto de integración con iniciar.py. Arranca TRES hilos de fondo
    (daemon): uno escucha activaciones (Pochonance seleccionó una cuenta),
    otro detenciones (Pochonance la deseleccionó), y un tercero que reporta
    periódicamente el estado real (heartbeat) para que el backend pueda
    corregir solo cualquier activación/detención que se haya perdido. Los
    tres usan el MISMO dict _sesiones_activas que ya usa el flujo de
    Telegram (se pasa por referencia — cualquier cuenta que arranque o pare
    acá también la ve el menú de Telegram, y viceversa). El nombre de la
    función se deja igual (iniciar_polling_activacion) para no tener que
    tocar de nuevo el único punto de integración en iniciar.py — ahora
    arranca los tres loops."""
    global _CHAT_ID_NOTIFICACION
    if chat_id_notificacion:
        _CHAT_ID_NOTIFICACION = chat_id_notificacion

    if NODO_ID != "A":
        if not getattr(_config, "BACKEND_SOPORTA_NODOS", False):
            print(f"⛔ [Nodo {NODO_ID}] BACKEND_SOPORTA_NODOS=False — NO se consultan las colas "
                  f"ni el latido de Pochonance (un backend sin enrutamiento por nodo le "
                  f"robaría las órdenes al nodo A). Actívalo en config.py solo cuando el "
                  f"backend ya lo soporte.")
            return None
        print(f"[Nodo {NODO_ID}] Pochonance: colas y latido con nodo={NODO_ID}")
    print(nodos.resumen())

    t1 = threading.Thread(
        target=_loop_polling,
        args=(sesiones_activas, actualizar_conteo_fn),
        daemon=True,
    )
    t1.start()

    t2 = threading.Thread(
        target=_loop_polling_detencion,
        args=(sesiones_activas, actualizar_conteo_fn),
        daemon=True,
    )
    t2.start()

    t3 = threading.Thread(
        target=_loop_heartbeat,
        args=(sesiones_activas,),
        daemon=True,
    )
    t3.start()

    # Comparte con Conciliación lo que ya lee de cada cuenta abierta (para verificar sus retiros sin que
    # Conciliación entre al banco con la misma cuenta). No hace nada extra en el banco. Ver pochonance_lecturas.py
    try:
        from Pochonance.pochonance_lecturas import iniciar_compartir_lecturas
        iniciar_compartir_lecturas(sesiones_activas, _API_BASE, _API_KEY_CUENTASP2P, _HEADERS_EXTRA, NODO_ID)
    except Exception as e:   # jamás debe impedir el arranque del monitoreo
        print(f"⚠️ [Lecturas] No se pudo iniciar: {e}")

    return t1
