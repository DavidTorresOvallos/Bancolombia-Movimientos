# -*- coding: utf-8 -*-
# =====================================================================
# CONSOLA.PY — que NINGÚN hilo se quede bloqueado por imprimir.
#
# Problema (14/10/2026): la consola clásica de Windows PAUSA el programa mientras
# hay texto seleccionado (basta con hacer clic dentro de la ventana). Como todas
# las cuentas escriben en esa consola, todos los hilos se bloqueaban a la vez, el
# watchdog (60 s) los veía "colgados" y mataba el Chrome de TODAS: seis reinicios
# simultáneos por algo tan simple como copiar el log.
#
# Solución: print() ya no escribe en la consola; deja la línea en una cola y vuelve
# al instante. Dos hilos aparte hacen la escritura:
#   - uno al ARCHIVO  logs/movimientos_<fecha>_nodo<ID>.log (siempre completo);
#   - uno a la CONSOLA, que si se bloquea solo se atrasa él: las líneas de más se
#     descartan de la PANTALLA (nunca del archivo) y al volver avisa cuántas.
#
# NO reemplaza sys.stdout (eso ya rompió la salida de los hilos en Windows, ver
# iniciar.py): se engancha en el print() que iniciar.py ya reemplaza.
#
# Solo usa la biblioteca estándar. Si algo falla, iniciar.py vuelve a imprimir
# directo, como antes.
# =====================================================================
import atexit
import collections
import glob
import os
import queue
import threading
import time

DIR_LOGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
DIAS_LOGS = 14
TOPE_COLA_CONSOLA = 2000      # líneas que aguanta la pantalla antes de descartar las más viejas
UMBRAL_BLOQUEO_S = 5          # una escritura a consola más lenta que esto se registra como bloqueo

_print_original = None
_activo = False
_q_archivo = queue.Queue(maxsize=50000)
_q_consola = queue.Queue(maxsize=TOPE_COLA_CONSOLA)
_recientes = collections.deque(maxlen=2000)   # para incidentes.py
_lock = threading.Lock()
_omitidas = [0]


def _nodo() -> str:
    try:
        import config
        return str(getattr(config, "NODO_ID", "A") or "A").strip().upper()
    except Exception:
        return "A"


def _ruta_log(fecha: str) -> str:
    return os.path.join(DIR_LOGS, f"movimientos_{fecha}_nodo{_nodo()}.log")


def _limpiar_viejos() -> None:
    limite = time.time() - DIAS_LOGS * 86400
    for ruta in glob.glob(os.path.join(DIR_LOGS, "movimientos_*_nodo*.log")):
        try:
            if os.path.getmtime(ruta) < limite:
                os.remove(ruta)
        except OSError:
            pass


def activo() -> bool:
    return _activo


def emitir(texto: str, end: str = "\n") -> None:
    """Llamado por print(). Nunca bloquea: solo encola."""
    linea = f"{texto}{end}"
    with _lock:
        _recientes.extend(texto.split("\n"))
    try:
        _q_archivo.put_nowait(linea)
    except queue.Full:
        pass                      # 50 000 líneas pendientes: el disco está muerto, no frenar las cuentas
    try:
        _q_consola.put_nowait(linea)
    except queue.Full:
        try:                      # pantalla atrasada: se descarta la más vieja
            _q_consola.get_nowait()
            _omitidas[0] += 1
        except queue.Empty:
            pass
        try:
            _q_consola.put_nowait(linea)
        except queue.Full:
            _omitidas[0] += 1


def ultimas_lineas(n: int = 300, contiene: str = None) -> list:
    """Últimas líneas impresas (de todas las cuentas), opcionalmente solo las que contienen un texto."""
    with _lock:
        lineas = list(_recientes)
    if contiene:
        lineas = [l for l in lineas if contiene in l]
    return lineas[-n:]


def _hilo_archivo() -> None:
    f = None
    fecha_actual = None
    while True:
        try:
            lote = [_q_archivo.get()]
            while len(lote) < 500:
                try:
                    lote.append(_q_archivo.get_nowait())
                except queue.Empty:
                    break
            fecha = time.strftime("%Y-%m-%d")
            if fecha != fecha_actual:
                if f:
                    f.close()
                os.makedirs(DIR_LOGS, exist_ok=True)
                f = open(_ruta_log(fecha), "a", encoding="utf-8", errors="replace")
                fecha_actual = fecha
                _limpiar_viejos()
            f.write("".join(lote))
            f.flush()
        except Exception:
            time.sleep(1)


def _hilo_consola() -> None:
    while True:
        linea = _q_consola.get()
        try:
            t0 = time.time()
            _print_original(linea, end="", flush=True)
            dt = time.time() - t0
            if dt >= UMBRAL_BLOQUEO_S:
                aviso = (f"[{time.strftime('%H:%M:%S')}] 🖥️ [consola] La pantalla estuvo bloqueada {dt:.0f}s "
                         f"(¿texto seleccionado en la ventana?). Las cuentas NO se detuvieron: "
                         f"todo está completo en logs/.")
                try:
                    _q_archivo.put_nowait(aviso + "\n")
                except queue.Full:
                    pass
                _print_original(aviso, flush=True)
            n = _omitidas[0]
            if n and _q_consola.empty():
                _omitidas[0] = 0
                _print_original(f"[{time.strftime('%H:%M:%S')}] 🖥️ [consola] {n} línea(s) no se mostraron en "
                                f"pantalla mientras estuvo bloqueada; están completas en logs/.", flush=True)
        except Exception:
            time.sleep(0.5)


def vaciar(timeout: float = 2.0) -> None:
    """Espera (poco) a que el archivo termine de escribir lo pendiente."""
    t0 = time.time()
    while not _q_archivo.empty() and time.time() - t0 < timeout:
        time.sleep(0.05)


def instalar(print_original) -> bool:
    """Arranca los dos hilos de escritura. print_original = el print real."""
    global _print_original, _activo
    if _activo:
        return True
    _print_original = print_original
    threading.Thread(target=_hilo_archivo, daemon=True, name="log-archivo").start()
    threading.Thread(target=_hilo_consola, daemon=True, name="log-consola").start()
    atexit.register(vaciar)
    _activo = True
    return True
