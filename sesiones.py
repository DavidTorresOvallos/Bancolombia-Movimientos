# -*- coding: utf-8 -*-
# =====================================================================
# SESIONES.PY
# Responsabilidad: ciclo de vida de sesiones y gestión de recursos.
# - MonitorThread: clase principal que orquesta los demás mixins
# - _sesion_completa(): lanza Chrome, login, startup, loop de monitoreo
# - run() / stop(): ciclo externo con reintentos y manejo de errores
# - _watchdog(): detecta cuelgues y fuerza reconexión
# - _esperar_memoria(), _memoria_sesion_mb(): gestión de RAM
# - _matar_chrome_zombie(): limpia procesos Chrome residuales
# =====================================================================
import os
import sys
import re
import time
import json
import threading
import subprocess
import random
import socket
import queue as _queue
from collections import Counter

# Nodo_B_Patchright: Patchright = Playwright con parches anti-detección (mismo API).
from patchright.sync_api import sync_playwright, Page as _PwPage, Frame as _PwFrame

# Patchright ejecuta page.evaluate en un "mundo aislado" (no ve variables JS de la página: más difícil
# de detectar). capsolver.py necesita el mundo REAL de la página (grecaptcha, window.__capsolver_token,
# ___grecaptcha_cfg). Sin tocar capsolver.py: los evaluate que mencionan esos nombres se ejecutan en el
# mundo de la página; todos los demás (solo DOM) siguen aislados.
_CLAVES_MUNDO_PAGINA = ("grecaptcha", "__capsolver", "___grecaptcha_cfg")


def _parchar_evaluate(clase):
    original = clase.evaluate

    def evaluate(self, expression, arg=None, **kw):
        if "isolated_context" not in kw and any(c in expression for c in _CLAVES_MUNDO_PAGINA):
            kw["isolated_context"] = False
        return original(self, expression, arg, **kw)
    clase.evaluate = evaluate


_parchar_evaluate(_PwPage)
_parchar_evaluate(_PwFrame)

from notificar import (
    bot, _enviar, _safe_send, OWNER_CHAT_ID, _parsear_pesos, _es_fatal, pausa_humana,
    NotificarMixin as _NotifMixin,
    bridge_conexion_exitosa, bridge_error_login,
)
from login import LoginMixin, URL_BANCO, SEÑAL_CAPTCHA_REQUIERE_CHROMIUM
from movimiento import (
    MovimientoMixin, extraer_saldo,
    API_TXS_KW, CACHE_DIR
)
# Bloqueo de telemetría de terceros (Dynatrace, VWO, Singular…) — ver ese archivo
from bloqueo_telemetria import es_telemetria_bloqueada
# Resolución de reCAPTCHA (CapSolver) — toda la lógica vive en capsolver.py.
# En Edge se resuelve por la API (Edge no puede montar la extensión); acá solo
# se usa HOOK_GRECAPTCHA_JS, que debe instalarse antes de cargar la página.
from capsolver import CAPSOLVER_EXT_PATH, HOOK_GRECAPTCHA_JS, _extension_capsolver_cargada
# Prueba de render con GPU / movimiento reducido en una cuenta — ver ese archivo
import importlib
import prueba_render
# Modo simulación (pruebas de carga; NADA llega a retiradores reales) — ver simulacion.py
import simulacion as _simmod
_SIM_ACTIVO = _simmod.activo()

# ── PRUEBA: recorrido sin pasar por Inicio (ver navegacion_sin_inicio.py) ──
# True  → Productos→Tus cuentas→Detalles→Movimientos (con recuperación automática)
# False → recorrido de siempre de movimiento.py
USAR_NAV_SIN_INICIO = True
if USAR_NAV_SIN_INICIO:
    from navegacion_sin_inicio import MovimientoSinInicioMixin as MovimientoMixin

# ── psutil (opcional) ──────────────────────────────────────────────
try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _psutil = None
    _PSUTIL_OK = False
    print("[⚠️] psutil no instalado — ejecuta: pip install psutil --break-system-packages")

# ── Constantes del sistema ─────────────────────────────────────────
# Navegador del carril normal. Se elige con  NAVEGADOR = "..."  en config.py (por defecto Edge):
#   "msedge"   → Microsoft Edge instalado en el PC
#   "chrome"   → Google Chrome instalado en el PC
#   "chromium" → el Chromium de Playwright, igual que el Nodo A (con su user-agent de Chrome)
# Cada navegador usa su propia carpeta de perfiles (no se mezclan). El carril de CapSolver sigue
# usando el Chromium de Playwright con la extensión, sea cual sea esta opción.
_NAVEGADORES = {"msedge": "Edge", "chrome": "Chrome", "chromium": "Chromium", "firefox": "Firefox"}
try:
    import config as _cfg_nav
    NAVEGADOR = str(getattr(_cfg_nav, "NAVEGADOR", "msedge") or "msedge").strip().lower()
except Exception:
    NAVEGADOR = "msedge"
if NAVEGADOR not in _NAVEGADORES:
    print(f"⚠️ NAVEGADOR='{NAVEGADOR}' no es válido en config.py (usa: msedge, chrome o chromium) — se usa msedge")
    NAVEGADOR = "msedge"

# Arranque directo en el Chromium de Playwright con la extensión de CapSolver
# montada (config.py → CAPSOLVER_DESDE_INICIO). Es el único carril donde las
# extensiones se cargan de verdad, así que evita el primer intento en Edge y el
# relanzamiento posterior cuando Bancolombia suelta el reCAPTCHA.
try:
    import config as _cfg_ext
    CAPSOLVER_DESDE_INICIO = bool(getattr(_cfg_ext, "CAPSOLVER_DESDE_INICIO", False))
except Exception:
    CAPSOLVER_DESDE_INICIO = False
HEADLESS             = False   # Bancolombia detecta y bloquea headless
REINICIO_CHROME_CADA = 40 * 60
# Cuentas que se detuvieron porque Bancolombia no mostró su menú tras entrar (pantalla rara/bloqueo):
# {nombre: instante hasta el que NO se vuelven a abrir}. Lo consulta pochonance_activador para que el
# latido no las reactive de inmediato y se repita el bucle (28/10).
COOLDOWN_CUENTAS: dict = {}
ENFRIAMIENTO_SIN_MENU_S = 10 * 60

# Reinicio por lentitud: la página de Bancolombia acumula memoria JS con cada
# recorrido del menú (medido 29/09/2026: JS heap 100→200 MB en ~4 min, cada
# clic 6-8× más lento) y la RAM no avisa a tiempo. Si la navegación completa
# tarda ≥ NAV_LENTA_S durante NAV_LENTA_CICLOS ciclos seguidos se reinicia
# Chrome por el mismo camino que el reinicio preventivo por RAM (el que se
# cumpla primero). Recargar la página NO sirve: Bancolombia cierra la sesión.
#
# Ajustables desde config.py. Con proxy/VPN la navegación normal sube a 15-27 s
# y con el umbral fijo de 12 s el bot reiniciaba varias cuentas cada pocos
# ciclos ("Reinicio preventivo por lentitud (nav 26s)" del 10/11 16:02). Cada
# reinicio es un login nuevo: eso fue lo que terminó bloqueando la IP.
def _cfg_int(nombre: str, defecto: int) -> int:
    try:
        return int(getattr(_cfg_nav, nombre, defecto))
    except Exception:
        return defecto


NAV_LENTA_S      = _cfg_int("NAV_LENTA_S", 12)
NAV_LENTA_CICLOS = _cfg_int("NAV_LENTA_CICLOS", 3)

# Tras decidir que un salto de saldo ya estaba notificado por la API, seguir
# recorriendo TODAS las páginas de hoy durante este tiempo: si Bancolombia
# publica tarde un movimiento real del mismo monto (en la página que sea),
# se detecta y notifica igual.
REPASO_PAGINAS_S = 60

if sys.platform == "win32":
    if NAVEGADOR == "firefox":
        USER_DATA_DIR = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "Mozilla", "Firefox", "Profiles", "BancolombiaBotFirefox"
        )
    else:
        USER_DATA_DIR = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "BancolombiaBot" + _NAVEGADORES[NAVEGADOR], "BancolombiaBot"   # perfiles propios por navegador
        )
else:
    if NAVEGADOR == "firefox":
        USER_DATA_DIR = os.path.expanduser(
            "~/Library/Application Support/Firefox/Profiles/BancolombiaBotFirefox"
        )
    else:
        USER_DATA_DIR = os.path.expanduser(
            "~/Library/Application Support/BancolombiaBot" + _NAVEGADORES[NAVEGADOR] + "/BancolombiaBot"
        )

# ── Turno de arranque: 1 cuenta abre Chrome + entra a Bancolombia a la vez ──
# Con tiempo máximo: si quien tiene el turno lleva más de TURNO_MAX_S sin
# soltarlo (arranque colgado — caso una cuenta 29/09 19:03, retuvo el turno
# ~3 min), la siguiente cuenta lo toma y avisa, en vez de esperar indefinida.
# Solo el titular actual puede soltarlo: si la cuenta colgada despierta
# después, no le quita el turno a otra.
TURNO_MAX_S = 90

# Firefox frena temporizadores y animación de las ventanas tapadas o en segundo plano (Chrome ya lleva sus
# equivalentes en _args_launch: --disable-background-timer-throttling, --disable-backgrounding-occluded-windows,
# --disable-renderer-backgrounding). Sin esto Playwright espera 20 s a que un botón sea "estable" y el SPA de
# Bancolombia tarda en pedir los movimientos (ciclos lentos, watchdog, "renderer muerto"). Solo afecta a Firefox.
_FIREFOX_PREFS = {
    "widget.windows.window_occlusion_tracking.enabled": False,   # no tratar como "oculta" una ventana tapada
    "dom.min_background_timeout_value": 4,                        # temporizadores sin freno en segundo plano
    "dom.min_background_timeout_value_without_budget_throttling": 4,
    "dom.timeout.enable_budget_timer_throttling": False,
    "dom.suspend_inactive.enabled": False,
    "browser.tabs.unloadOnLowMemory": False,
}
_turno_lock = threading.Lock()
_turno: dict = {"titular": None, "desde": 0.0, "no_antes": 0.0}

# Ritmo humano de logins (04/11/2026): con 4 cuentas entrando una cada ~10 s todo fue bien y la 5ª recibió
# «Debes esperar…» (5 logins en 43 s desde el mismo PC). Tras soltar el turno, la SIGUIENTE cuenta espera
# un tiempo al azar entre LOGIN_ESPACIO_MIN_S y LOGIN_ESPACIO_MAX_S antes de poder entrar, como si varias
# personas fueran sentándose a usar el mismo computador. Se ajusta en config.py (LOGIN_ESPACIO_MIN_S / _MAX_S);
# 0 y 0 = sin espacio (comportamiento anterior).
try:
    import config as _cfg_ritmo
    LOGIN_ESPACIO_MIN_S = float(getattr(_cfg_ritmo, "LOGIN_ESPACIO_MIN_S", 30))
    LOGIN_ESPACIO_MAX_S = float(getattr(_cfg_ritmo, "LOGIN_ESPACIO_MAX_S", 60))
except Exception:
    LOGIN_ESPACIO_MIN_S, LOGIN_ESPACIO_MAX_S = 30.0, 60.0

# ── VPN / proxy de salida (pruebas) ────────────────────────────────
# Hace que SOLO el navegador de Bancolombia salga por un proxy local — el
# resto del PC (Telegram, Pochonance, CapSolver) sigue con su conexión normal.
# Pensado para Cloudflare WARP en modo proxy: WARP escucha en 127.0.0.1:40000
# y Playwright le pasa 'proxy=' al contexto. Se configura en config.py
# (USAR_PROXY_VPN, PROXY_VPN_SERVER...) y se prepara con:  python vpn_warp.py
# Si el proxy no responde NO se rompe nada: se abre sin VPN y se avisa una vez.
try:
    import config as _cfg_vpn
    USAR_PROXY_VPN    = bool(getattr(_cfg_vpn, "USAR_PROXY_VPN", False))
    PROXY_VPN_SERVER  = str(getattr(_cfg_vpn, "PROXY_VPN_SERVER", "http://127.0.0.1:40000") or "").strip()
    PROXY_VPN_USUARIO = str(getattr(_cfg_vpn, "PROXY_VPN_USUARIO", "") or "").strip()
    PROXY_VPN_CLAVE   = str(getattr(_cfg_vpn, "PROXY_VPN_CLAVE", "") or "")
except Exception:
    USAR_PROXY_VPN, PROXY_VPN_SERVER, PROXY_VPN_USUARIO, PROXY_VPN_CLAVE = False, "", "", ""

_vpn_aviso_dado = False

def _proxy_vpn_alcance(server: str, timeout: float = 1.5) -> bool:
    """True si el proxy local (p. ej. Cloudflare WARP) acepta conexiones."""
    try:
        from urllib.parse import urlparse
        u = urlparse(server if "://" in server else "http://" + server)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        if not host or not port:
            return False
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False

def _proxy_vpn_opts(nombre: str = "") -> dict:
    """Devuelve {'proxy': {...}} para launch_persistent_context, o {} si está
    apagado o si el proxy no responde (nunca tumba la sesión por la VPN)."""
    global _vpn_aviso_dado
    if not USAR_PROXY_VPN or not PROXY_VPN_SERVER:
        return {}
    if not _proxy_vpn_alcance(PROXY_VPN_SERVER):
        if not _vpn_aviso_dado:
            _vpn_aviso_dado = True
            print(f"[VPN] ⚠️ {PROXY_VPN_SERVER} no responde — se abre SIN VPN. "
                  f"Levántalo con: python vpn_warp.py")
        return {}
    _p = {"server": PROXY_VPN_SERVER}
    if PROXY_VPN_USUARIO:
        _p["username"] = PROXY_VPN_USUARIO
        _p["password"] = PROXY_VPN_CLAVE
    print(f"[{nombre or 'VPN'}] 🌍 Salida por proxy/VPN: {PROXY_VPN_SERVER}")
    return {"proxy": _p}

# Quién espera turno y cuántos logins seguidos lleva fallando. Una cuenta que
# viene fallando (≥ 2) cede el turno a las que no, para no dejarlas ciegas
# (10/10: una cuenta falló 6 veces y tomaba el turno en cada reintento).
_esperando_turno: dict = {}   # {nombre: (fallos_login_seguidos, última_consulta)}

def _tomar_turno(nombre: str, fallos_login: int = 0, primer_login: bool = False):
    """None = ocupado. "" = tomado libre. "<nombre>" = tomado a quien lo retenía de más.
    primer_login=True → además respeta el ritmo humano entre primeros logins (LOGIN_ESPACIO_*)."""
    with _turno_lock:
        titular, ahora = _turno["titular"], time.time()
        _esperando_turno[nombre] = (fallos_login, ahora)
        if primer_login and titular is None and ahora < _turno["no_antes"]:
            return None   # ritmo humano: aún no toca otro primer login en este PC
        if titular is None or ahora - _turno["desde"] > TURNO_MAX_S:
            if fallos_login >= 2 and any(
                f < fallos_login and ahora - t < 5
                for n, (f, t) in _esperando_turno.items() if n != nombre
            ):
                return None   # hay otra esperando que no viene fallando: va primero
            _esperando_turno.pop(nombre, None)
            _turno["titular"], _turno["desde"] = nombre, ahora
            return titular or ""
        return None

def _dejar_de_esperar_turno(nombre: str):
    with _turno_lock:
        _esperando_turno.pop(nombre, None)

def _soltar_turno(nombre: str, primer_login: bool = False):
    with _turno_lock:
        if _turno["titular"] == nombre:
            _turno["titular"] = None
            if primer_login:   # un re-login no cuenta para el ritmo de los primeros logins
                _turno["no_antes"] = time.time() + random.uniform(
                    min(LOGIN_ESPACIO_MIN_S, LOGIN_ESPACIO_MAX_S), max(LOGIN_ESPACIO_MIN_S, LOGIN_ESPACIO_MAX_S))

# ── Cuentas arrancando: prioridad de entrada y reinicios escalonados ──
# Una cuenta está "arrancando" desde que pide turno (recién activada o
# reconectando) hasta su primer ciclo de monitoreo completo. Mientras haya
# alguna arrancando, las que ya trabajan NO se reinician por lentitud:
# siguen vigilando (salvo nav ≥ NAV_CRITICA_S), así las que llegan entran
# primero. Y solo UNA cuenta a la vez se reinicia por lentitud → quedan
# escalonadas solas aunque se pongan lentas al mismo tiempo.
NAV_CRITICA_S     = _cfg_int("NAV_CRITICA_S", 25)   # por encima de esto se reinicia igual (antes del watchdog de 60s)
_ARRANQUE_VENCE_S = 150    # marca vieja (hilo que murió sin desmarcar) → se ignora
_arrancando: dict = {}     # {nombre: timestamp}
_arrancando_lock  = threading.Lock()

def _limpiar_arrancando_vencidas():
    ahora = time.time()
    for _n, _t in list(_arrancando.items()):
        if ahora - _t > _ARRANQUE_VENCE_S:
            del _arrancando[_n]

def _marcar_arrancando(nombre: str):
    with _arrancando_lock:
        _arrancando[nombre] = time.time()

def _desmarcar_arrancando(nombre: str):
    with _arrancando_lock:
        _arrancando.pop(nombre, None)

def _otras_arrancando(nombre: str) -> list:
    with _arrancando_lock:
        _limpiar_arrancando_vencidas()
        return [n.strip() for n in _arrancando if n != nombre]

def _reservar_reinicio(nombre: str) -> bool:
    """Si nadie más está arrancando, se marca y devuelve True (puede reiniciar)."""
    with _arrancando_lock:
        _limpiar_arrancando_vencidas()
        if any(n != nombre for n in _arrancando):
            return False
        _arrancando[nombre] = time.time()
        return True

# ── Límite de RAM dinámico según cantidad de sesiones activas ──────
# Con POCAS sesiones Chrome tiene más RAM disponible y la usa más → límite alto.
# Con MUCHAS sesiones hay más presión de OS y cada Chrome usa menos → límite menor.
# Observado en logs: 3 sesiones → Chrome llega a 1714 MB; 6 sesiones → ~1484 MB.
# Fórmula: 1900 - (n-1)*50 → 1:1900, 2:1850, 3:1800, 4:1750, 5:1700, 6:1650
# Mínimo de 1650 para que con 6+ sesiones aún haya margen sobre los ~1484 MB reales.
_sesiones_activas_count: int = 1

def actualizar_conteo_sesiones(n: int):
    """Llamada por iniciar.py cuando el número de sesiones activas cambia."""
    global _sesiones_activas_count
    _sesiones_activas_count = max(1, int(n))

def _ram_limite_mb() -> int:
    """Límite de RAM por sesión según la cantidad de sesiones activas.
    Escala HACIA ABAJO con más sesiones: pocas sesiones → cada Chrome puede
    usar más RAM (menos presión de OS); muchas sesiones → más presión → menos RAM.
    """
    return max(1650, 1900 - (_sesiones_activas_count - 1) * 50)

# ── Windows: minimizar ventanas Chrome nuevas ─────────────────────
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wt
    def _obtener_hwnds_chrome() -> set:
        _u32 = ctypes.windll.user32
        _hwnds: set = set()
        @ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)
        def _cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            _u32.GetClassNameW(hwnd, buf, 256)
            if buf.value == "Chrome_WidgetWin_1" and _u32.IsWindowVisible(hwnd):
                if _u32.GetWindowTextLengthW(hwnd) > 0:
                    _hwnds.add(hwnd)
            return True
        _u32.EnumWindows(_cb, 0)
        return _hwnds
    def _minimizar_nuevas_ventanas_chrome(hwnds_antes: set, intentos: int = 20):
        """Mueve nuevas ventanas Chrome FUERA DE PANTALLA en vez de minimizarlas.
        Razón: SW_SHOWMINIMIZED hace que Chrome ponga visibilityState='hidden' a
        nivel de navegador (antes de que cualquier JS corra), lo cual dispara el
        modal '¿Cerrar sesión?' de Bancolombia en cada startup/reinicio de RAM.
        Una ventana fuera de pantalla (-9999,0) NO está minimizada → Chrome
        mantiene visibilityState='visible' → Bancolombia no detecta inactividad."""
        _u32 = ctypes.windll.user32
        SWP_NOZORDER   = 0x0004
        SWP_NOACTIVATE = 0x0010
        for _ in range(intentos):
            nuevas = _obtener_hwnds_chrome() - hwnds_antes
            if nuevas:
                time.sleep(0.4)
                for hwnd in nuevas:
                    # Primero restaurar si estaba minimizado de sesión anterior
                    _u32.ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE: visible sin activar
                    # Mover fuera de pantalla (no minimizar — Chrome conserva 'visible')
                    _u32.SetWindowPos(
                        hwnd, 0,
                        -9999, 0, 1280, 900,
                        SWP_NOZORDER | SWP_NOACTIVATE
                    )
                return
            time.sleep(0.3)
else:
    def _obtener_hwnds_chrome() -> set: return set()
    def _minimizar_nuevas_ventanas_chrome(hwnds_antes: set, intentos: int = 20): pass

# ══════════════════════════════════════════════════════════════════
# MonitorThread
# Combina todos los mixins en una sola clase que corre en su propio
# thread. Cada cuenta Bancolombia tiene su propia instancia.
# ══════════════════════════════════════════════════════════════════
class MonitorThread(_NotifMixin, MovimientoMixin, LoginMixin, threading.Thread):

    INTERVALO_TOGGLE     = 1        # segundos entre ciclos
    INTERVALO_FULL_NAV   = 10 * 60  # navegación completa cada 10 min (keepalive)
    FALLOS_PARA_FULL     = 3        # fallos de toggle antes de forzar full nav

    def __init__(self, nombre: str, usuario: str, contrasena: str, chat_id: int):
        threading.Thread.__init__(self, daemon=True)
        self.nombre     = nombre
        self.usuario    = usuario
        self.contrasena = contrasena
        self.chat_id    = chat_id
        self.running    = True

        # Estado de transacciones
        self.txs_conocidas: dict = {}

        # Startup
        self.startup_done        = threading.Event()
        self.startup_ok          = False
        self.saldo_inicial       = None
        self.saldo_actual        = None
        self.saldo_en_ultimo_movimiento = None
        self.primera_data_event  = threading.Event()
        self.primera_data_txs:  list = []
        self.primera_data_saldo: str = "—"
        # Motivo del último fallo de login (lo llena LoginMixin._login()) —
        # usado para avisarle a Pochonance por qué no se pudo activar la cuenta.
        self._motivo_login_fallido: str | None = None

        # CapSolver (dos carriles): Edge no puede montar la extensión
        # (--load-extension fue retirado), así que el carril normal es Edge sin
        # extensión. Cuando el login detecta un reCAPTCHA pone este flag en
        # True y el PRÓXIMO arranque usa el Chromium de Playwright + la
        # extensión (donde sí se monta y cliva el checkbox, marcando el captcha
        # como resuelto en el frontend de Bancolombia).
        self._captcha_requiere_chromium: bool = False
        # True si el arranque actual montó la extensión (lo decide
        # _sesion_completa; lo lee login.py para saber qué flujo usar).
        self._capsolver_extension: bool = False

        # Estado interno
        self._saldo_en_detalles  = None
        self._pausado            = threading.Event()
        self._pausado.set()
        self._tx_api_queue: _queue.Queue = _queue.Queue()
        self._renderer_muerto    = threading.Event()
        self._context            = None
        self._playwright         = None
        self._fallos_seguidos    = 0
        self._dom_filas_counter: dict = {}
        self._api_campos_logueados   = False
        self._api_url_logueada       = False
        self._ultimo_saldo_notificado: str | None = None
        self._last_snapshot: list = []
        # Cuándo se leyó por última vez el banco (la API de movimientos). Lo usa pochonance_lecturas.py para
        # compartir la lectura con Conciliación SOLO si es reciente.
        self._ultima_lectura_ts: float = 0.0
        self._dom_hora_logueado  = False
        self._ciclos_skip_dom: int = 0
        self._ultimo_latido: float = time.time()
        self._ultimo_alerta_watchdog: float = 0.0
        self._baseline_ok: bool = False
        # Medición de tiempo ciego: desde que la cuenta se activa o se cae
        # hasta su primer ciclo de monitoreo completo. None = viendo normal.
        self._t_ciega_desde: float | None = time.time()
        self._motivo_ciega: str = "arranque"
        # Detención suave (ver detener_suave): el loop termina el ciclo en curso
        self._detener_pedido: bool = False
        self._en_loop: bool = False
        # Montos que la API notificó ANTES de que el saldo de Detalles se moviera
        # [(timestamp, monto)]. Cuando el saldo salta por ese monto en un ciclo
        # posterior, ya está notificado → no se repite por DOM ni por "cambio de
        # saldo" (duplicado real: una cuenta 02/10 15:52/15:53).
        self._xhr_sin_saldo: list = []
        self._repasar_paginas_hasta: float = 0.0   # ver REPASO_PAGINAS_S

        # ── Movimientos del día (snapshot bajo demanda) ───────────────
        self._snapshot_solicitado: threading.Event = threading.Event()
        self._snapshot_chat_id: int | None = None

        safe = re.sub(r"[^\w]", "_", nombre)
        base = os.path.dirname(USER_DATA_DIR)
        self.profile_dir = os.path.join(base, f"Monitor_{safe}")
        self._cargar_cache()

    @property
    def movimientos_conocidos(self) -> int:
        return sum(self.txs_conocidas.values())

    @property
    def esta_pausado(self) -> bool:
        return not self._pausado.is_set()

    def pausar(self, minutos: int = 10):
        self._pausado.clear()
        if minutos > 0:
            def _auto():
                time.sleep(minutos * 60)
                if not self._pausado.is_set():
                    self._pausado.set()
                    _enviar(self.chat_id,
                            f"▶️ *{self.nombre}* reanudado automáticamente.",
                            parse_mode="Markdown")
            threading.Thread(target=_auto, daemon=True).start()

    def reanudar(self):
        self._pausado.set()

    def _dirs_perfil(self) -> list:
        """Carpetas de perfil de esta cuenta: la normal y la de Chromium+extensión.
        Con CAPSOLVER_DESDE_INICIO se usa _chromium en TODOS los arranques, así que
        los locks/parcheos tienen que cubrir las dos (si no, queda 'profile locked')."""
        return [self.profile_dir, self.profile_dir + "_chromium"]

    # ── RAM del Chrome de esta cuenta ─────────────────────────────
    def _memoria_sesion_mb(self) -> float:
        if not _PSUTIL_OK:
            return 0.0
        try:
            perfil_lower = self.profile_dir.replace('\\', '/').lower()
            pids: set = set()
            total = 0
            for proc in _psutil.process_iter(['name', 'cmdline', 'pid', 'memory_info']):
                try:
                    n = (proc.info.get('name') or '').lower()
                    if 'chrome' not in n and 'chromium' not in n and 'msedge' not in n:
                        continue
                    cmd = ' '.join(proc.info.get('cmdline') or []).replace('\\', '/').lower()
                    if perfil_lower not in cmd:
                        continue
                    pid = proc.info['pid']
                    if pid not in pids:
                        pids.add(pid)
                        total += proc.info['memory_info'].rss
                except (_psutil.NoSuchProcess, _psutil.AccessDenied, Exception):
                    pass
            return total / (1024 * 1024)
        except Exception:
            return 0.0

    def _esperar_memoria(self, min_mb: float = 600, intentos: int = 8):
        """Espera hasta que haya `min_mb` MB disponibles antes de lanzar Chrome."""
        def _ram_disponible_mb() -> float:
            if _PSUTIL_OK:
                return _psutil.virtual_memory().available / (1024 * 1024)
            if sys.platform == 'win32':
                try:
                    r = subprocess.run(
                        ['wmic', 'os', 'get', 'FreePhysicalMemory', '/value'],
                        capture_output=True, text=True, timeout=5
                    )
                    for line in r.stdout.splitlines():
                        if line.lower().startswith('freephysicalmemory='):
                            kb_str = line.split('=', 1)[1].strip()
                            if kb_str.isdigit():
                                return int(kb_str) / 1024
                except Exception:
                    pass
            return min_mb  # si no podemos medir, asumir suficiente

        for i in range(intentos):
            disponible = _ram_disponible_mb()
            if disponible >= min_mb:
                return
            print(f"[{self.nombre}] ⚠️ RAM disponible: {disponible:.0f} MB "
                  f"(mínimo {min_mb:.0f}) — esperando 8s ({i+1}/{intentos})")
            self._ultimo_latido = time.time()
            time.sleep(8)
        disponible = _ram_disponible_mb()
        print(f"[{self.nombre}] ⚠️ Lanzando Chrome con {disponible:.0f} MB (espera agotada)")

    def _matar_chrome_zombie(self):
        """
        Elimina procesos Chrome del perfil de esta cuenta.
        Limpia locks SIEMPRE (evita 'profile locked' en el próximo arranque).
        """
        for _dir_perfil in self._dirs_perfil():
            for _lock in ("SingletonLock", "lockfile", ".parentlock"):
                _lp = os.path.join(_dir_perfil, _lock)
                try:
                    if os.path.exists(_lp):
                        os.remove(_lp)
                        print(f"[{self.nombre}] 🔓 Lock eliminado: {_lock}")
                except Exception:
                    pass

        if not _PSUTIL_OK:
            if sys.platform == 'win32':
                try:
                    perfil_esc = self.profile_dir.replace("'", "''")
                    result = subprocess.run(
                        ['wmic', 'process', 'where',
                         f"(Name like '%chrome%' or Name like '%chromium%' or Name like '%msedge%') and "
                         f"CommandLine like '%{perfil_esc}%'",
                         'get', 'ProcessId', '/format:list'],
                        capture_output=True, text=True, timeout=10
                    )
                    muertos = 0
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if line.lower().startswith('processid='):
                            pid_str = line.split('=', 1)[1].strip()
                            if pid_str.isdigit():
                                subprocess.run(
                                    ['taskkill', '/F', '/T', '/PID', pid_str],
                                    capture_output=True, timeout=5
                                )
                                print(f"[{self.nombre}] 🔪 Chrome PID {pid_str} eliminado (wmic)")
                                muertos += 1
                    if muertos:
                        time.sleep(8.0)
                except Exception as e:
                    print(f"[{self.nombre}] ⚠️ _matar_chrome_zombie (wmic): {e}")
            return

        try:
            perfil_lower = self.profile_dir.replace('\\', '/').lower()
            muertos = 0
            for proc in _psutil.process_iter(['name', 'cmdline', 'pid']):
                try:
                    n = (proc.info.get('name') or '').lower()
                    if 'chrome' not in n and 'chromium' not in n and 'msedge' not in n:
                        continue
                    cmd = ' '.join(proc.info.get('cmdline') or []).replace('\\', '/').lower()
                    if perfil_lower not in cmd:
                        continue
                    proc.kill()
                    muertos += 1
                    print(f"[{self.nombre}] 🔪 Chrome PID {proc.info['pid']} eliminado")
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            if muertos:
                time.sleep(8.0)
        except Exception as e:
            print(f"[{self.nombre}] ⚠️ _matar_chrome_zombie: {e}")

    # ── Watchdog ───────────────────────────────────────────────────
    def _watchdog(self):
        """
        Thread independiente que detecta cuelgues (page.evaluate bloqueado por OOM)
        y fuerza reconexión matando Chrome directamente.
        NO usa ctx.close() — Playwright usa greenlets y llamarlo desde otro thread
        lanza greenlet.error corrompiendo el estado.
        """
        MAX_SILENCIO = 60
        CHECK_CADA   = 10
        while self.running:
            for _ in range(CHECK_CADA):
                if not self.running:
                    return
                time.sleep(1)
            if not self._pausado.is_set():
                self._ultimo_latido = time.time()
                continue
            delta = time.time() - self._ultimo_latido
            if delta > MAX_SILENCIO:
                print(f"[{self.nombre}] 🐕 Watchdog: {delta:.0f}s sin latido — reconectando"
                      f"  [pasos nav: {' | '.join(getattr(self, '_nav_pasos', []))}]")
                _ahora_wd = time.time()
                if _ahora_wd - self._ultimo_alerta_watchdog > 300:
                    self._ultimo_alerta_watchdog = _ahora_wd
                    _safe_send(
                        OWNER_CHAT_ID,
                        f"⚠️ *{self.nombre}* — Watchdog: colgado {delta:.0f}s → reconectando",
                        parse_mode="Markdown"
                    )
                try:   # incidentes.py: deja los datos del cuelgue (sin página: otro hilo no puede tocar Playwright)
                    import incidentes
                    incidentes.registrar(self, None, "watchdog", detalle=f"{delta:.0f}s sin latido")
                except Exception:
                    pass
                self._renderer_muerto.set()
                self._matar_chrome_zombie()
                self._ultimo_latido = time.time()

    # ── Sesión completa ────────────────────────────────────────────
    def _sesion_completa(self):
        """
        Lanza Chrome, hace login, establece baseline y entra en el loop de monitoreo.
        Retorna normalmente si login falla. Lanza excepción si Chrome crashea.
        run() maneja ambos casos.
        """
        self._parchar_prefs()
        self._ultimo_latido = time.time()

        # Serializar el arranque: solo 1 cuenta lanza Chrome a la vez
        _marcar_arrancando(self.nombre)   # las demás posponen reinicios por lentitud
        print(f"[{self.nombre}] ⏳ Esperando turno de arranque...")
        # El ritmo humano entre logins solo vale para el PRIMER login de la cuenta (startup_done aún sin marcar);
        # los re-logins de una cuenta que ya estaba monitoreando siguen como siempre.
        _primer_login = not self.startup_done.is_set()
        while True:
            _retenido_por = _tomar_turno(self.nombre, getattr(self, "_fallos_login_seguidos", 0), _primer_login)
            if _retenido_por is not None:
                break
            self._ultimo_latido = time.time()
            if not self.running:
                _dejar_de_esperar_turno(self.nombre)
                return
            time.sleep(1)
        if _retenido_por:
            print(f"[{self.nombre}] ⚠️ Turno de arranque retenido por "
                  f"{_retenido_por.strip()} más de {TURNO_MAX_S}s — se continúa sin esperar")
        print(f"[{self.nombre}] 🎟️ Turno de arranque tomado")
        _sem_liberado = False

        def _liberar_sem():
            nonlocal _sem_liberado
            if not _sem_liberado:
                _sem_liberado = True
                _soltar_turno(self.nombre, _primer_login)

        if not self.running:
            _liberar_sem()
            return

        p       = None
        context = None
        _hwnds_antes = _obtener_hwnds_chrome()

        try:
            try:
                p = sync_playwright().start()
            except Exception as _e_pw:
                print(f"[{self.nombre}] ⚠️ Playwright start falló ({_e_pw}) — reintentando en 3s")
                time.sleep(3)
                p = sync_playwright().start()
            self._playwright = p

            _ARGS = [
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-default-apps",
                "--js-flags=--max-old-space-size=450 --expose-gc",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=TranslateUI,BlinkGenPropertyTrees,Translate",
                "--disable-sync",
                "--no-default-browser-check",
                "--disable-logging",
                "--log-level=3",
                "--memory-pressure-off",
                "--aggressive-cache-discard",
                "--renderer-process-limit=1",
                "--disk-cache-size=52428800",
                "--media-cache-size=10485760",
                "--disable-application-cache",
            ]
            _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/137.0.0.0 Safari/537.36")

            self._matar_chrome_zombie()
            self._esperar_memoria(min_mb=600)
            if not self.running:
                print(f"[{self.nombre}] 🛑 Detenida mientras arrancaba — no se abre Chrome")
                return

            # Prueba de render: se relee el archivo en cada arranque para poder
            # cambiar la cuenta de prueba sin reiniciar el sistema.
            _args_launch, _opts_launch = _ARGS, {}
            try:
                importlib.reload(prueba_render)
                _args_launch = prueba_render.ajustar_args(self.nombre, _ARGS)
                _opts_launch = prueba_render.opciones_contexto(self.nombre)
                if prueba_render.en_prueba(self.nombre):
                    print(f"[{self.nombre}] 🧪 Prueba render: GPU activada + movimiento reducido")
            except Exception as _e_pr:
                _args_launch, _opts_launch = _ARGS, {}
                print(f"[{self.nombre}] ⚠️ prueba_render no se pudo leer ({_e_pr}) — Chrome normal")

            # Latido continuo durante arranque + login: ambas fases pueden tomar
            # 20-40s cada una. Sin este thread el watchdog dispara en reconexiones
            # post-reinicio de RAM donde Chrome arranca limpio y el login es lento.
            _arranque_ok = threading.Event()
            def _hb_launch():
                while not _arranque_ok.is_set():
                    self._ultimo_latido = time.time()
                    _arranque_ok.wait(timeout=5)
            threading.Thread(target=_hb_launch, daemon=True).start()
            # ── CapSolver: carril de extensión ──
            # Edge/Chrome instalados ya no permiten montar extensiones
            # (--load-extension fue retirado), así que el único navegador que
            # carga capsolver_extension de verdad es el Chromium de Playwright.
            #
            # CAPSOLVER_DESDE_INICIO=True (config.py) → TODAS las sesiones abren
            # ahí desde el principio: la extensión clica el checkbox y el
            # frontend de Bancolombia lo marca resuelto (la API queda de respaldo).
            # False → dos carriles como antes: Edge primero y, solo si el login
            # detecta reCAPTCHA (self._captcha_requiere_chromium), el siguiente
            # arranque usa Chromium + extensión.
            _con_ext = bool(
                (CAPSOLVER_DESDE_INICIO or self._captcha_requiere_chromium)
                and NAVEGADOR != "firefox"
                and os.path.isdir(CAPSOLVER_EXT_PATH)
            )
            if _con_ext:
                _args_launch = list(_args_launch) + [
                    f"--disable-extensions-except={CAPSOLVER_EXT_PATH}",
                    f"--load-extension={CAPSOLVER_EXT_PATH}",
                ]
                _ignore = ["--enable-automation", "--disable-extensions"]
                _dir    = self.profile_dir + "_chromium"
                _chan   = None   # None → Chromium de Playwright (donde sí monta)
                if self._captcha_requiere_chromium:
                    print(f"[{self.nombre}] 🧩 reCAPTCHA → abriendo Chromium + CapSolver (carril extensión)")
                else:
                    print(f"[{self.nombre}] 🧩 Abriendo Chromium + CapSolver (extensión desde el inicio)")
            else:
                if CAPSOLVER_DESDE_INICIO:
                    print(f"[{self.nombre}] ⚠️ CAPSOLVER_DESDE_INICIO=True pero no existe "
                          f"{CAPSOLVER_EXT_PATH} — se abre el carril normal sin extensión.")
                _ignore = ["--enable-automation"]   # navegador sin extensión
                _dir    = self.profile_dir
                _chan   = None if NAVEGADOR == "chromium" else NAVEGADOR   # None → Chromium de Playwright
                print(f"[{self.nombre}] 🌐 Abriendo {_NAVEGADORES[NAVEGADOR]}...")
            self._capsolver_extension = _con_ext

            # Con el Chromium de Playwright (igual que el Nodo A) se usa el mismo user-agent de Chrome;
            # Edge y Chrome instalados se presentan con el suyo.
            _opts = dict(_opts_launch)
            if NAVEGADOR == "chromium" and not _con_ext:
                _opts["user_agent"] = _UA
            # VPN/proxy de salida (opcional, config.py → USAR_PROXY_VPN).
            # Aplica a los DOS carriles: Edge/Chrome normal y Chromium+extensión.
            _opts.update(_proxy_vpn_opts(self.nombre))
            try:
                if NAVEGADOR == "firefox":
                    os.makedirs(self.profile_dir, exist_ok=True)
                    context = p.firefox.launch_persistent_context(
                        user_data_dir=self.profile_dir,
                        headless=HEADLESS,
                        firefox_user_prefs=_FIREFOX_PREFS,
                    )
                else:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=_dir,
                        channel=_chan,
                        headless=HEADLESS,
                        args=_args_launch,
                        ignore_default_args=_ignore,
                        **_opts,
                    )
            except Exception:
                _arranque_ok.set()   # detener thread si Chrome no pudo lanzar
                raise
            self._context = context
            # Verificar que la extensión quedó montada (solo carril extensión).
            if _con_ext:
                if _extension_capsolver_cargada(context):
                    print(f"[{self.nombre}] [CapSolver] Extensión montada OK.")
                else:
                    print(f"[{self.nombre}] ⚠️ [CapSolver] La extensión NO se montó "
                          f"— revisa capsolver_extension (manifest.json, background.js).")
            self._renderer_muerto.clear()
            threading.Thread(
                target=_minimizar_nuevas_ventanas_chrome,
                args=(_hwnds_antes,), daemon=True
            ).start()

            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(20000)
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            # Hook de reCAPTCHA (CapSolver): se instala ANTES de que cargue la
            # página, para que grecaptcha.getResponse()/execute() devuelvan el
            # token inyectado cuando Angular envíe el formulario de login.
            page.add_init_script(HOOK_GRECAPTCHA_JS)
            # Simular presencia de usuario: Bancolombia no puede detectar que
            # Chrome está en segundo plano (document.hasFocus/visibilityState).
            # Este script se inyecta ANTES que cualquier JS de Bancolombia para
            # que sus listeners vean siempre 'visible' / hasFocus() === true.
            #
            # TÉCNICA CLAVE: se intercepta addEventListener para reemplazar
            # los handlers de 'visibilitychange' de Bancolombia con no-ops
            # ANTES de que los registren. Los eventos nativos del OS (minimize)
            # siguen disparándose, pero los handlers no hacen nada.
            # dispatchEvent no sirve aquí porque los eventos del OS saltan el JS.
            page.add_init_script("""
                (function() {
                    try {
                        // 1. Propiedades de visibilidad siempre 'visible'
                        Object.defineProperty(document, 'visibilityState', {
                            configurable: true, get: function() { return 'visible'; }
                        });
                        Object.defineProperty(document, 'hidden', {
                            configurable: true, get: function() { return false; }
                        });
                        document.hasFocus = function() { return true; };

                        // 2. Interceptar addEventListener para neutralizar
                        //    cualquier handler de 'visibilitychange' y 'blur'/'focusout'
                        //    que Bancolombia intente registrar.
                        //    Se usa UNA sola función vacía compartida: el navegador
                        //    ignora registros idénticos, así no se acumulan no-ops
                        //    que Bancolombia luego no puede quitar (29/09/2026).
                        if (EventTarget.prototype.addEventListener.__bcPresencia) return;
                        const _noop = window.__bcNoop = window.__bcNoop || function() {};
                        const _origAEL = EventTarget.prototype.addEventListener;
                        EventTarget.prototype.addEventListener = function(type, handler, options) {
                            if (type === 'visibilitychange' || type === 'blur' || type === 'focusout') {
                                return _origAEL.call(this, type, _noop, options);
                            }
                            return _origAEL.call(this, type, handler, options);
                        };
                        EventTarget.prototype.addEventListener.__bcPresencia = true;
                    } catch(e) {}
                })();
            """)
            # Aplicar overrides también a la página ya cargada (persistent context
            # puede tener una página en estado cacheado que no recibe el init script
            # hasta la siguiente navegación — esta llamada la cubre de inmediato).
            try:
                page.evaluate("""() => {
                    try {
                        Object.defineProperty(document, 'visibilityState', {
                            configurable: true, get: function() { return 'visible'; }
                        });
                        Object.defineProperty(document, 'hidden', {
                            configurable: true, get: function() { return false; }
                        });
                        document.hasFocus = function() { return true; };
                        if (EventTarget.prototype.addEventListener.__bcPresencia) return;
                        const _noop = window.__bcNoop = window.__bcNoop || function() {};
                        const _origAEL = EventTarget.prototype.addEventListener;
                        EventTarget.prototype.addEventListener = function(type, handler, options) {
                            if (type === 'visibilitychange' || type === 'blur' || type === 'focusout') {
                                return _origAEL.call(this, type, _noop, options);
                            }
                            return _origAEL.call(this, type, handler, options);
                        };
                        EventTarget.prototype.addEventListener.__bcPresencia = true;
                    } catch(e) {}
                }""")
            except Exception:
                pass

            # Diagnóstico: solicitudes de red por ciclo y cuáles se repiten más
            # (se reinician en cada resumen 🔬 del loop de monitoreo).
            self._diag_req_total = 0
            self._diag_req_urls  = Counter()

            # Bloquear recursos innecesarios (imágenes, analytics, fonts)
            def _bloquear(route):
                url = route.request.url.lower()
                self._diag_req_total += 1
                self._diag_req_urls[url.split("?")[0][-70:]] += 1
                if es_telemetria_bloqueada(self.nombre, url):
                    route.abort(); return
                if any(url.endswith(ext) for ext in (
                    ".png",".jpg",".jpeg",".gif",".svg",".ico",".webp",
                    ".mp4",".webm",".woff",".woff2",".ttf",".eot"
                )):
                    route.abort(); return
                if any(s in url for s in (
                    "google-analytics","googletagmanager","hotjar",
                    "doubleclick","facebook.net","newrelic","datadog",
                    "sentry.io","amplitude","mixpanel",
                )):
                    route.abort(); return
                route.continue_()
            page.route("**/*", _bloquear)

            # Registrar interceptores ANTES de navegar
            page.on("response", self._on_api_response)
            page.route(f"**{API_TXS_KW}**", self._route_ampliar_txs)
            page.on("crash", lambda: (
                self._renderer_muerto.set(),
                print(f"[{self.nombre}] 💥 page.crash")
            ))

            # ── Login ──
            if not self.running:
                _arranque_ok.set()   # apagar el hilo de latido del arranque
                print(f"[{self.nombre}] 🛑 Detenida mientras arrancaba — no se entra a Bancolombia")
                return
            print(f"[{self.nombre}] 🔑 Entrando a Bancolombia...")
            # El turno se suelta apenas se envía el formulario (login.py llama
            # este gancho): la espera de hasta 45 s a que Bancolombia responda
            # ya no frena el arranque de las demás (10/10: un login fallido
            # retenía el turno ~90 s y dejaba cuentas ciegas 4-10 min).
            self._al_enviar_login = _liberar_sem
            login_ok = self._login(page)
            _arranque_ok.set()   # detener thread de latido: login completado
            _liberar_sem()
            if not login_ok:
                motivo_fallido = getattr(self, "_motivo_login_fallido", None)
                # Solo se relanza si de verdad veníamos SIN extensión y la carpeta
                # existe: evita un bucle Edge→Chromium→Edge si la extensión falta.
                if (motivo_fallido == SEÑAL_CAPTCHA_REQUIERE_CHROMIUM
                        and not getattr(self, "_capsolver_extension", False)
                        and os.path.isdir(CAPSOLVER_EXT_PATH)):
                    try:
                        self._captcha_requiere_chromium = True
                    except Exception:
                        pass
                    print(f"[{self.nombre}] 🧩 reCAPTCHA en carril Edge → cerrando y relanzando con Chromium + CapSolver extension...")
                    try:
                        if context:
                            try:
                                context.close()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if p:
                            try:
                                p.stop()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    self._context = None
                    self._playwright = None
                    try:
                        self._matar_chrome_zombie()
                    except Exception:
                        pass
                    time.sleep(2)
                    # Relanzar con carril extensión
                    try:
                        self._marcar_arrancando(self.nombre)
                    except Exception:
                        pass
                    return self._sesion_completa()
                # Primer intento de esta cuenta (recién activada) y Bancolombia
                # mostró un aviso explícito de bloqueo/cancelación: no tiene
                # sentido reintentar — se avisa a Pochonance de una vez y se
                # detiene, en vez de esperar a agotar los reintentos normales.
                if not self.startup_done.is_set() and motivo_fallido:
                    self.startup_ok = False
                    self.startup_done.set()
                    self.running = False
                    bridge_error_login(self.nombre, motivo_fallido)
                return
            # Login OK: se resolvió (o no hubo captcha). El próximo arranque
            # vuelve a intentar con Edge (carril normal); solo si aparece un
            # reCAPTCHA se usará Chromium+extensión de nuevo.
            self._captcha_requiere_chromium = False
            if len(context.pages) > 1:
                page = context.pages[-1]
            self._ultimo_latido = time.time()

            # ── Navegación de startup (Productos → Tus cuentas → Movimientos) ──
            self._navegar_rapido_a_movimientos(page)
            saldo = self._saldo_en_detalles or "—"
            # Re-inyectar overrides de presencia tras la navegación de startup.
            # Bancolombia puede registrar nuevos listeners al cargar la página de
            # Movimientos; esta llamada refuerza los overrides en el estado actual.
            try:
                page.evaluate("""() => {
                    try {
                        Object.defineProperty(document, 'visibilityState', {
                            configurable: true, get: function() { return 'visible'; }
                        });
                        Object.defineProperty(document, 'hidden', {
                            configurable: true, get: function() { return false; }
                        });
                        document.hasFocus = function() { return true; };
                    } catch(e) {}
                }""")
            except Exception:
                pass
            self._ultimo_latido = time.time()

            # ── Esperar primera respuesta de la API ──
            # 8s es suficiente: el XHR llega durante la navegación de startup.
            # Si no llegó en 8s no llegará hasta el próximo nav → pasar al loop.
            txs_init = self._esperar_api(timeout=8.0, page=page)
            if not txs_init:
                txs_init = self._drenar_queue()  # último intento: cola residual

            es_primera_sesion = not self.startup_done.is_set()

            # ── Establecer baseline o detectar txs perdidas en reconexión ──
            if txs_init:
                if es_primera_sesion:
                    # Primera sesión: registrar todo como conocido.
                    # Paginamos SOLO UNA VEZ y guardamos el resultado en
                    # _txs_ext para reutilizarlo luego en primera_data_txs
                    # (segunda paginación redundante eliminada — ver fix
                    # 19/09/2026: doble paginación al startup causaba que el
                    # watchdog disparara en cuentas con muchos movimientos).
                    self._ultimo_latido = time.time()   # evitar watchdog durante la paginación
                    _txs_ext = self._recopilar_txs_hoy(page, txs_init)
                    self._ultimo_latido = time.time()   # actualizar tras terminar
                    if len(_txs_ext) > len(txs_init):
                        print(f"[{self.nombre}] 📋 Baseline extendido: {len(txs_init)} → "
                              f"{len(_txs_ext)} txs (páginas 2+)")
                        txs_init = _txs_ext
                        try:
                            self._click_tab_por_texto(page, "Detalles")
                            time.sleep(0.2)
                            # Al mostrar la pestaña Detalles Bancolombia lanza el tutorial de comprobantes (overlay que tapa
                            # todo): se cierra ANTES de pulsar Movimientos, si no ese clic espera 20 s por selector
                            # (una cuenta 19/10: 40 s perdidos + watchdog a los 66 s).
                            time.sleep(0.5); self._cerrar_modales(page)
                            self._click_tab_por_texto(page, "Movimientos")
                            time.sleep(0.5)
                            self._drenar_queue()
                        except Exception:
                            pass
                    self._actualizar_conocidas(txs_init)
                    self._last_snapshot = list(txs_init)
                    self._ultima_lectura_ts = time.time()
                    self._baseline_ok = True
                    print(f"[{self.nombre}] 📋 Baseline: {len(txs_init)} txs conocidas")
                else:
                    # Reconexión: usar baseline_ok guard para evitar flood histórico
                    if not self._baseline_ok:
                        # El watchdog disparó antes de que se estableciera el baseline
                        self._actualizar_conocidas(txs_init)
                        self._last_snapshot = list(txs_init)
                        self._ultima_lectura_ts = time.time()
                        self._baseline_ok = True
                        print(f"[{self.nombre}] 📋 Baseline (primer arranque recuperado): "
                              f"{len(txs_init)} txs")
                    else:
                        # Reconexión normal: diff contra snapshot pre-caída
                        saldo_ant_recon = self.saldo_en_ultimo_movimiento or self.saldo_actual or "—"
                        nuevas_recon = (
                            self._diff_snapshot(txs_init, self._last_snapshot)
                            if self._last_snapshot else
                            self._detectar_nuevas(txs_init)
                        )
                        metodo = "snapshot" if self._last_snapshot else "counter"
                        # Solo hoy + ayer (dias=1): evitar re-notificar txs históricas
                        nuevas_recon = [
                            tx for tx in nuevas_recon
                            if self._es_fecha_reciente(tx.get("transactionDate", ""), dias=1)
                        ]
                        if nuevas_recon:
                            print(f"[{self.nombre}] 🔄 Reconexión ({metodo}): "
                                  f"{len(nuevas_recon)} txs perdidas")
                            base_val_recon = _parsear_pesos(saldo_ant_recon) or 0.0
                            sc_recon = base_val_recon
                            _batch_recon: list = []
                            for tx_obj in reversed(nuevas_recon):
                                monto_tx = float(tx_obj.get("amount", 0))
                                s_ant = "$ " + f"{int(round(sc_recon)):,}".replace(",", ".")
                                sc_recon += monto_tx
                                s_act = "$ " + f"{int(round(sc_recon)):,}".replace(",", ".")
                                _batch_recon.append((tx_obj, s_ant, s_act))
                            def _enviar_batch_recon(_b=_batch_recon):
                                for _t, _sa, _sact in _b:
                                    self._notificar_tx(_t, _sa, _sact)
                            threading.Thread(target=_enviar_batch_recon, daemon=True).start()
                            # Verificar saldo calculado vs real
                            sc_final = base_val_recon + sum(
                                float(t.get("amount", 0)) for t in nuevas_recon
                            )
                            val_real_r = _parsear_pesos(saldo)
                            if val_real_r is not None and abs(val_real_r - sc_final) >= 1:
                                delta_ix = val_real_r - sc_final
                                saldo_esp = "$ " + f"{int(round(sc_final)):,}".replace(",", ".")
                                print(f"[{self.nombre}] ⚠️ Reconexión: saldo calculado "
                                      f"{saldo_esp} ≠ real {saldo} (Δ={delta_ix:+.0f}) "
                                      f"→ tx fuera de ventana")
                                threading.Thread(
                                    target=self._notificar_saldo_cambio_inferido,
                                    args=(txs_init, saldo_esp, saldo, delta_ix),
                                    daemon=True
                                ).start()
                            self.saldo_en_ultimo_movimiento = saldo
                        else:
                            print(f"[{self.nombre}] ✅ Reconexión ({metodo}): "
                                  f"sin txs perdidas")
                        self._last_snapshot = list(txs_init)
                        self._ultima_lectura_ts = time.time()
                        self._actualizar_conocidas(txs_init)

            else:
                # API no respondió en el startup
                if es_primera_sesion:
                    print(f"[{self.nombre}] ⚠️ Startup: API sin respuesta — baseline pendiente")
                else:
                    if not self._baseline_ok:
                        print(f"[{self.nombre}] ⚠️ Startup: API sin respuesta (sin baseline)")
                    else:
                        val_n = _parsear_pesos(saldo)
                        val_a = _parsear_pesos(self.saldo_actual) if self.saldo_actual else None
                        if val_n and val_a and abs(val_n - val_a) >= 1:
                            delta = val_n - val_a
                            print(f"[{self.nombre}] ⚠️ Reconexión sin API: "
                                  f"saldo cambió (Δ={delta:+.0f})")
                            threading.Thread(
                                target=self._notificar_saldo_cambio_inferido,
                                args=([], self.saldo_actual or "—", saldo, delta),
                                daemon=True
                            ).start()

            # Baseline DOM para fallback
            # NOTA: en el startup la página puede mostrar el selector de fechas
            # (calendario) en vez de la lista de transacciones. _scrape_dom_filas
            # devuelve esas filas de calendario ("DomLunMar...", "1234..."), que
            # son inútiles para el counter. Si se agregan al counter, _fallback_dom
            # luego devuelve TODAS las txs históricas como "nuevas" causando spam.
            # Solución: filtrar filas que parezcan calendario antes de agregarlas.
            import re as _re_dom
            # Filas del datepicker ('arrow2-left | Septiembre 2026 | DomLunMar… | 1234…').
            # OJO: NO filtrar por nombre de mes — las filas reales también lo
            # tienen ("29 sept 2026", "01 oct 2026") y así se descartaban TODAS
            # (foto DOM siempre vacía desde el 28/09 → spam de filas históricas,
            # corregido 01/10/2026). Las filas reales siempre traen un monto "$";
            # el calendario nunca.
            _PATRON_CALENDARIO = _re_dom.compile(
                r'(Dom\s*Lun|Lun\s*Mar\s*Mi|1\s*2\s*3\s*4\s*5|arrow2-left|arrow2-right)',
                _re_dom.IGNORECASE
            )
            try:
                filas_ini = self._scrape_dom_filas(page)
                if filas_ini:
                    # Filtrar filas de calendario que no corresponden a txs reales
                    filas_tx = [f for f in filas_ini
                                if "$" in f and not _PATRON_CALENDARIO.search(f)]
                    filas_descartadas = len(filas_ini) - len(filas_tx)
                    if filas_descartadas:
                        print(f"[{self.nombre}] 🗂️  DOM baseline: {len(filas_ini)} filas "
                              f"({filas_descartadas} de calendario descartadas → "
                              f"{len(filas_tx)} txs reales)")
                    else:
                        print(f"[{self.nombre}] 🗂️  DOM baseline: {len(filas_ini)} filas")
                    for f in filas_tx:
                        self._dom_filas_counter[f] = self._dom_filas_counter.get(f, 0) + 1
                    if not self._dom_hora_logueado:
                        self._dom_hora_logueado = True
                        _patron_hora = _re_dom.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b')
                        horas_encontradas = [
                            m.group(0)
                            for f in filas_ini[:5]
                            for m in [_patron_hora.search(f)] if m
                        ]
                        if horas_encontradas:
                            print(f"[{self.nombre}] ⏰ DOM con horas: {horas_encontradas}")
                        else:
                            muestra = filas_ini[0][:120] if filas_ini else "—"
                            print(f"[{self.nombre}] ⏰ DOM sin horas — muestra: {muestra!r}")
            except Exception:
                pass

            # Señalar startup al primer arranque
            if es_primera_sesion:
                # txs_init ya fue extendido por _recopilar_txs_hoy en el bloque
                # de baseline de arriba (líneas ~510-526) — reusar directamente en
                # vez de paginar de nuevo. Llamar _recopilar_txs_hoy dos veces
                # duplicaba el tiempo de startup y disparaba el watchdog en cuentas
                # con muchos movimientos (bug detectado 19/09/2026).
                # Filtrar solo las txs de HOY (la API devuelve los últimos 20 sin
                # importar fecha — esto excluía ayer del startup pero los mostraba
                # como "Movimientos de hoy", bug detectado 21/09/2026).
                hoy_str = time.strftime("%Y/%m/%d")
                txs_hoy_startup = (
                    [t for t in txs_init if t.get("transactionDate", "") == hoy_str]
                    if txs_init else []
                )
                self.primera_data_txs   = txs_hoy_startup
                self.primera_data_saldo = saldo
                # Setar evento cuando la API respondió, incluso si hoy no hay txs:
                # iniciar.py muestra "Sin movimientos hoy" de inmediato en vez de
                # esperar el timeout de 150 s.
                if txs_init:
                    self.primera_data_event.set()
                self.startup_ok = True
                self.startup_done.set()
                # Avisar a Pochonance si la API respondió — si txs_init vino
                # vacío (API lenta en el startup), se difiere al loop.
                if txs_init:
                    bridge_conexion_exitosa(self.nombre, saldo, txs_hoy_startup)

            # ═══════════════════════════════════════════════
            # LOOP DE MONITOREO
            # primera_vez = True cuando NO existe todavía un baseline (ni del
            # startup ni de un ciclo anterior). En ese caso el primer ciclo del
            # loop construye el baseline SIN notificar, para evitar el flood
            # histórico (todas las txs de hoy saldrían como "nuevas").
            # Si ya hay baseline (_baseline_ok), txs_conocidas filtra lo conocido
            # y el primer ciclo notifica solo lo genuinamente nuevo.
            #
            # 10/10: antes era `es_primera_sesion and txs_init is None`, así que
            # una RECONEXIÓN que nunca logró baseline (arranques fallidos seguidos
            # por CPU saturada → 0 conteos conocidos) notificaba las ~20 txs del
            # día de golpe. Caso de una cuenta: 18 avisos duplicados a las 18:04.
            # Con `not self._baseline_ok` esa reconexión construye baseline en
            # silencio; una reconexión que SÍ tenía baseline sigue notificando el
            # diff (no se pierde nada que haya entrado mientras estuvo ciega).
            # ═══════════════════════════════════════════════
            primera_vez      = (txs_init is None) and not self._baseline_ok
            # _saltar_nav: el primer ciclo omite _click_buscar porque ya
            # estamos en Movimientos tras la navegación de startup.
            # Solo hace el toggle Detalles→Movimientos para re-disparar el XHR.
            # En reconexión sin datos de startup usamos full nav directamente:
            # el toggle no re-dispara el XHR con fiabilidad tras un reinicio.
            _saltar_nav      = es_primera_sesion or (txs_init is not None)
            inicio_sesion    = time.time()
            ultimo_keepalive = time.time()
            INTERVALO_KEEPALIVE = 20
            _ciclos_gc  = 0
            _diag_ciclos = 0   # ciclos desde el último resumen 🔬
            _navs_lentas = 0   # navegaciones seguidas ≥ NAV_LENTA_S
            _ultima_nav  = 0.0 # duración de la última navegación (umbral crítico)
            _aviso_pospuesto = False   # ya se avisó que el reinicio está en espera
            GC_CADA_CICLOS = 30
            _ciclos_presencia = 0
            PRESENCIA_CADA    = 4   # simular actividad cada ~4 ciclos (~20s)

            self._en_loop = True
            while self.running:
                self._ultimo_latido = time.time()
                _t_ciclo = time.time()   # medición: duración total del ciclo

                # Detención suave: el ciclo anterior ya terminó (y notificó) → salir
                if self._detener_pedido:
                    print(f"[{self.nombre}] 🛑 Detención: ciclo en curso terminado — cerrando")
                    self.running = False
                    break

                # Pausada
                if not self._pausado.is_set():
                    raise Exception(f"Cuenta {self.nombre} pausada — sesión cerrada")

                # Reinicio preventivo por tiempo
                if time.time() - inicio_sesion >= REINICIO_CHROME_CADA:
                    raise Exception("Reinicio preventivo de memoria")

                # Reinicio preventivo por lentitud (se revisa al empezar el ciclo
                # siguiente, así lo detectado en el ciclo lento ya salió a notificarse).
                # Escalonado: si otra cuenta está arrancando se pospone y se sigue
                # vigilando, salvo que la navegación ya sea crítica.
                if _navs_lentas >= NAV_LENTA_CICLOS:
                    _critica = _ultima_nav >= NAV_CRITICA_S
                    if _critica or _reservar_reinicio(self.nombre):
                        _marcar_arrancando(self.nombre)
                        _umbral = (f"crítica {_ultima_nav:.0f}s" if _critica
                                   else f"≥ {NAV_LENTA_S}s × {_navs_lentas} ciclos seguidos")
                        raise Exception(f"Reinicio preventivo por lentitud (nav {_umbral})")
                    if not _aviso_pospuesto:
                        _aviso_pospuesto = True
                        print(f"[{self.nombre}] ⏸️ Reinicio por lentitud en espera — "
                              f"arrancando: {', '.join(_otras_arrancando(self.nombre)) or 'otra cuenta'}"
                              f" (sigo vigilando; se fuerza si nav ≥ {NAV_CRITICA_S}s)")

                # Renderer muerto
                if self._renderer_muerto.is_set():
                    raise Exception("Renderer muerto — reconectando")

                # Sesión expirada por URL
                try:
                    url_actual = page.url
                    if "autenticacion" in url_actual:
                        raise Exception("Sesión expirada (URL)")
                    if "firmar-documentos" in url_actual:
                        self._aceptar_documentos_si_aparece(page)
                    popup_info = page.evaluate("""() => {
                        const txt = (document.body?.innerText || '').toLowerCase();
                        if (txt.includes('out of memory') ||
                            (txt.includes('vaya') && txt.includes('error al mostrar'))) {
                            return 'oom';
                        }
                        if (txt.includes('cerramos la sesi') ||
                            (txt.includes('su sesi') && txt.includes('expir'))) {
                            return 'session_closed';
                        }
                        if (txt.includes('sin actividad') ||
                            (txt.includes('algo sali') && txt.includes('999'))) {
                            const btn = [...document.querySelectorAll('button, a')].find(
                                b => /intentar|nuevamente|reintentar/i.test(b.textContent||'')
                            );
                            if (btn) { btn.click(); return 'retried'; }
                            return 'session_closed';
                        }
                        return null;
                    }""")
                    if popup_info == 'oom':
                        _safe_send(
                            OWNER_CHAT_ID,
                            f"⚠️ *{self.nombre}* — Out of Memory en Chrome\n"
                            f"Cerrando instancia y reconectando...",
                            parse_mode="Markdown"
                        )
                        raise Exception("OOM del renderer → reconectando")
                    elif popup_info == 'session_closed':
                        raise Exception("Sesión cerrada por Bancolombia → reconectando")
                    elif popup_info == 'retried':
                        print(f"[{self.nombre}] ⚠️ Popup inactividad — click OK automático")
                        time.sleep(2)
                except Exception as ex_url:
                    if any(k in str(ex_url).lower() for k in
                           ("expirada", "cerrada", "reconectando")) or _es_fatal(ex_url):
                        raise

                # Latido tras el bloque URL/popup: page.url y page.evaluate pueden
                # bloquearse > 60s cuando Chrome está bajo presión de RAM, haciendo
                # que el watchdog dispare aunque la sesión está viva.
                self._ultimo_latido = time.time()

                _modal_result = self._cerrar_modales(page)
                if _modal_result == 'kept_session':
                    # Bancolombia mostró "¿Cerrar sesión?" y clickeamos NO.
                    # _cerrar_modales ya navegó al home; forzamos una iteración
                    # nueva para que _click_buscar haga la navegación completa
                    # Inicio→Productos→Movimientos desde un estado limpio.
                    self._ultimo_latido = time.time()
                    _saltar_nav = False   # no usar el toggle de startup
                    print(f"[{self.nombre}] 🔄 Reiniciando ciclo de monitoreo tras '¿Cerrar sesión?'")
                    continue

                # Simulación de presencia de usuario (mousemove suave)
                # Previene que Bancolombia considere la sesión "inactiva" cuando
                # Chrome está en segundo plano entre reinicio de RAM y reconexión.
                _ciclos_presencia += 1
                if _ciclos_presencia >= PRESENCIA_CADA:
                    _ciclos_presencia = 0
                    try:
                        page.evaluate("""() => {
                            try {
                                document.dispatchEvent(new MouseEvent('mousemove', {
                                    bubbles: true, cancelable: true,
                                    clientX: 200 + Math.round(Math.random() * 200),
                                    clientY: 300 + Math.round(Math.random() * 150),
                                    movementX: Math.round(Math.random() * 6 - 3),
                                    movementY: Math.round(Math.random() * 6 - 3)
                                }));
                            } catch(e) {}
                        }""")
                    except Exception:
                        pass

                # Keepalive
                ahora = time.time()
                if ahora - ultimo_keepalive >= INTERVALO_KEEPALIVE:
                    try:
                        vp = page.viewport_size or {"width": 1280, "height": 720}
                        page.mouse.move(
                            vp["width"]  // 2 + random.randint(-80, 80),
                            vp["height"] // 2 + random.randint(-80, 80)
                        )
                    except Exception:
                        pass
                    ultimo_keepalive = ahora

                saldo_anterior = self.saldo_actual

                # Guardar respuesta rezagada del ciclo anterior como fallback
                txs_rezagadas = self._drenar_queue()
                if txs_rezagadas:
                    print(f"[{self.nombre}] ♻️  Respuesta rezagada guardada "
                          f"({len(txs_rezagadas)} txs)")

                _t_nav = time.time()     # medición: duración de la navegación
                self._nav_pasos = []     # _click_buscar lo llena paso a paso
                _nav_ciclo = False       # este ciclo navegó con _click_buscar
                if _saltar_nav:
                    # Primer ciclo: ya estamos en Movimientos desde el startup.
                    # Solo toggle Detalles→Movimientos para re-disparar el XHR
                    # sin recorrer toda la sidebar (ahorra 10-15s por ciclo).
                    _saltar_nav = False
                    nav_ok = True
                    try:
                        # Si el arranque NO llegó a "Detalles y movimientos" (la página se quedó en Inicio porque
                        # Bancolombia tardó en responder), el toggle no hace nada y esperar la API son 25 s perdidos
                        # (una cuenta 30/10, otra cuenta 29/10: 78 s ciegas). En ese caso se va directo al recorrido completo.
                        if "/detalles-y-movimientos/" not in (page.url or ""):
                            print(f"[{self.nombre}] ↪️ El arranque no llegó a Detalles y movimientos "
                                  f"({page.url}) — recorrido completo en vez del toggle")
                            raise RuntimeError("arranque sin llegar a Detalles y movimientos")
                        self._click_tab_por_texto(page, "Detalles")
                        time.sleep(0.15)
                        # Al mostrar la pestaña Detalles Bancolombia lanza el tutorial de comprobantes (overlay que tapa
                        # todo): se cierra ANTES de pulsar Movimientos, si no ese clic espera 20 s por selector
                        # (una cuenta 19/10: 40 s perdidos + watchdog a los 66 s).
                        time.sleep(0.5); self._cerrar_modales(page)
                        self._click_tab_por_texto(page, "Movimientos")
                        print(f"[{self.nombre}] ⚡ Startup directo — toggle Detalles→Movimientos")
                    except Exception:
                        nav_ok = self._click_buscar(page)
                else:
                    nav_ok = self._click_buscar(page)
                    _nav_ciclo = True
                _dur_nav = time.time() - _t_nav
                if _nav_ciclo:
                    # Umbral de lentitud (no cuenta el toggle de arranque)
                    _navs_lentas = _navs_lentas + 1 if _dur_nav >= NAV_LENTA_S else 0
                    _ultima_nav  = _dur_nav
                    if _navs_lentas == 0:
                        _aviso_pospuesto = False
                if not nav_ok:
                    print(f"[{self.nombre}] ⏱️ Navegación falló tras {_dur_nav:.1f}s"
                          f"  [{' | '.join(self._nav_pasos)}]")
                    time.sleep(2)
                    continue

                _t_api = time.time()     # medición: espera de la respuesta XHR
                txs = self._esperar_api(timeout=25.0, page=page)
                _dur_api = time.time() - _t_api

                # Paginar si el saldo cambió, o en el primer ciclo para baseline completo.
                # Bancolombia NO ordena los movimientos cronológicamente — los agrupa
                # por tipo: primero salidas, luego transferencias, y al final NEQUI.
                # Esto significa que un NEQUI nuevo puede quedar en página 2 o 3
                # aunque haya llegado hace segundos (si hay 20+ movimientos de otros
                # tipos antes). Sin paginación al cambio de saldo, esas txs nunca
                # se detectarían. El baseline extendido del startup (ver arriba) cubre
                # las txs HISTÓRICAS de pág. 2 para que no aparezcan como nuevas;
                # la paginación aquí es para ENCONTRAR la tx nueva que puede estar
                # enterrada en pág. 2+ por el orden por tipo de Bancolombia.
                if txs:
                    _val_n_pag = _parsear_pesos(self._saldo_en_detalles or "")
                    _val_a_pag = _parsear_pesos(saldo_anterior or "")
                    _saldo_cambio_pag = (
                        _val_n_pag is not None and _val_a_pag is not None
                        and abs(_val_n_pag - _val_a_pag) >= 1
                    )
                    # Repaso: tras dar un salto de saldo por "ya notificado", seguir
                    # recorriendo todas las páginas de hoy unos segundos por si
                    # Bancolombia publica tarde un movimiento real del mismo monto.
                    _en_repaso = time.time() < self._repasar_paginas_hasta
                    if _saldo_cambio_pag or primera_vez or _en_repaso:
                        _txs_pre = len(txs)
                        txs = self._recopilar_txs_hoy(page, txs)
                        if primera_vez and len(txs) > _txs_pre:
                            print(f"[{self.nombre}] 📋 Baseline extendido en loop: "
                                  f"{_txs_pre} → {len(txs)} txs")
                        # Si primera_vez=True y el XHR ya explicó el cambio de saldo,
                        # marcar _ciclos_skip_dom=1 para que el ciclo 2 no dispare el
                        # fallback DOM sobre el mismo cambio (el baseline se construyó
                        # con la tx nueva, pero _ciclos_skip_dom queda en 0 porque el
                        # bloque `if nuevas_txs and not primera_vez` nunca se ejecuta).
                        if primera_vez and _saldo_cambio_pag and txs:
                            self._ciclos_skip_dom = 1

                if txs is None:
                    print(f"[{self.nombre}] ⚠️ Sin respuesta API este ciclo "
                          f"(nav {_dur_nav:.1f}s + esperó API {_dur_api:.1f}s)"
                          f"  [{' | '.join(self._nav_pasos)}]")
                    if txs_rezagadas:
                        print(f"[{self.nombre}] ♻️  Usando API rezagada ({len(txs_rezagadas)} txs)")
                        txs = txs_rezagadas
                    else:
                        if not primera_vez:
                            val_n = _parsear_pesos(self._saldo_en_detalles or "")
                            val_a = _parsear_pesos(saldo_anterior) if saldo_anterior else None
                            if val_n and val_a and abs(val_n - val_a) >= 1:
                                saldo_dom = self._saldo_en_detalles or saldo_anterior or "—"
                                print(f"[{self.nombre}] 🚨 Saldo cambió + API timeout → DOM")
                                n_dom, filas_dom = self._fallback_dom(
                                    page, saldo_anterior, saldo_dom)
                                if n_dom:
                                    for _fila in filas_dom:
                                        threading.Thread(
                                            target=self._notificar_dom_alerta,
                                            args=(_fila, saldo_anterior, saldo_dom),
                                            daemon=True
                                        ).start()
                                    self.saldo_en_ultimo_movimiento = saldo_dom
                                    self.saldo_actual = saldo_dom
                                    self._guardar_cache()
                        time.sleep(2)
                        continue

                # ── Snapshot bajo demanda (Movimientos del día) ──
                # IMPORTANTE: _recopilar_txs_hoy usa `page` → debe correr
                # en este mismo hilo. Solo el envío Telegram va en daemon.
                if self._snapshot_solicitado.is_set():
                    self._snapshot_solicitado.clear()
                    snap_chat_id = self._snapshot_chat_id
                    try:
                        todas_hoy = self._recopilar_txs_hoy(page, list(txs))
                        self._guardar_snapshot_diario(todas_hoy)
                        threading.Thread(
                            target=self._notificar_movimientos_hoy,
                            args=(todas_hoy, snap_chat_id),
                            daemon=True
                        ).start()
                    except Exception as _e:
                        print(f"[{self.nombre}] ⚠️ snapshot_hoy: {_e}")

                # ── MODO SIMULACIÓN: meter movimientos artificiales ──
                # Solo si el interruptor está encendido (ver simulacion.py).
                # Se prependen a txs con fecha de hoy y huella única, así la
                # detección normal los ve como nuevos y los notifica por el
                # mismo camino real (pero las notificaciones ya van desviadas
                # al bot de prueba / archivo, nunca a retiradores).
                if _SIM_ACTIVO:
                    _iny = _simmod.tomar_inyectados(self.nombre)
                    if _iny:
                        txs = list(_iny) + list(txs or [])
                        print(f"[{self.nombre}] 💉 {len(_iny)} movimiento(s) inyectado(s) (SIMULACIÓN)")

                # ── Detectar nuevas txs ──
                nuevas_txs  = self._detectar_nuevas(txs)
                saldo_actual = self._saldo_en_detalles or saldo_anterior
                n_conocidas  = self._n_conocidas_total(self.txs_conocidas)

                # Filtro 1: descartar txs históricas >3 días
                if nuevas_txs:
                    filtradas = [
                        tx for tx in nuevas_txs
                        if not self._es_fecha_reciente(tx.get("transactionDate",""), dias=3)
                    ]
                    if filtradas:
                        descs = [
                            f"{t.get('transactionDate','')} {(t.get('description') or '')[:20]}"
                            for t in filtradas
                        ]
                        print(f"[{self.nombre}] 🗑️  {len(filtradas)} txs históricas: {descs}")
                    nuevas_txs = [
                        tx for tx in nuevas_txs
                        if self._es_fecha_reciente(tx.get("transactionDate",""), dias=3)
                    ]

                # Filtro 2: sin cambio de saldo → solo txs de hoy
                if nuevas_txs:
                    _sn = _parsear_pesos(saldo_actual)
                    _sa = _parsear_pesos(saldo_anterior) if saldo_anterior else None
                    saldo_cambio_ciclo = bool(_sn and _sa and abs(_sn - _sa) >= 1)
                    if not saldo_cambio_ciclo:
                        antes_sg = len(nuevas_txs)
                        nuevas_txs = [
                            tx for tx in nuevas_txs
                            if self._es_fecha_reciente(tx.get("transactionDate",""), dias=0)
                        ]
                        descartadas_sg = antes_sg - len(nuevas_txs)
                        if descartadas_sg:
                            print(f"[{self.nombre}] 🔕 {descartadas_sg} tx(s) descartadas "
                                  f"(saldo sin cambio → non-determinism)")

                print(f"[{self.nombre}] 🔍 {len(txs)} txs | {n_conocidas} conteos conocidos"
                      f"{f' | {len(nuevas_txs)} NUEVA(S)' if nuevas_txs else ''}"
                      f" | ⏱️ ciclo {time.time() - _t_ciclo:.1f}s"
                      f" (nav {_dur_nav:.1f}s, API {_dur_api:.1f}s)")
                if self._t_ciega_desde is not None:
                    print(f"[{self.nombre}] ⏱️ Cuenta ciega {time.time() - self._t_ciega_desde:.0f}s "
                          f"(motivo: {self._motivo_ciega}) — ya monitoreando de nuevo")
                    self._t_ciega_desde = None
                # Primer ciclo completo → ya no cuenta como "arrancando"
                _desmarcar_arrancando(self.nombre)

                if nuevas_txs and not primera_vez:
                    base_val = _parsear_pesos(
                        self.saldo_en_ultimo_movimiento or saldo_anterior or ""
                    ) or 0.0
                    saldo_corriente = base_val
                    _batch_normal: list = []
                    for tx in reversed(nuevas_txs):
                        monto_tx = float(tx.get("amount", 0))
                        s_ant_str = "$ " + f"{int(round(saldo_corriente)):,}".replace(",", ".")
                        saldo_corriente += monto_tx
                        s_act_str = "$ " + f"{int(round(saldo_corriente)):,}".replace(",", ".")
                        _batch_normal.append((tx, s_ant_str, s_act_str))
                    def _enviar_batch_normal(_b=_batch_normal):
                        for _t, _sa, _sact in _b:
                            self._notificar_tx(_t, _sa, _sact)
                    threading.Thread(target=_enviar_batch_normal, daemon=True).start()
                    self.saldo_en_ultimo_movimiento = (
                        "$ " + f"{int(round(saldo_corriente)):,}".replace(",", ".")
                    )
                    self._ciclos_skip_dom = 1
                    # ¿El saldo de Detalles todavía no reflejaba estas txs? → recordarlas
                    _vn_x = _parsear_pesos(saldo_actual)
                    _va_x = _parsear_pesos(saldo_anterior) if saldo_anterior else None
                    if _vn_x is not None and _va_x is not None and abs(_vn_x - _va_x) < 1:
                        _ahora_x = time.time()
                        self._xhr_sin_saldo.extend(
                            (_ahora_x, float(_t.get("amount", 0))) for _t in nuevas_txs)

                elif not primera_vez:
                    val_n = _parsear_pesos(saldo_actual)
                    val_a = _parsear_pesos(saldo_anterior) if saldo_anterior else None
                    saldo_cambio = (val_n is not None and val_a is not None
                                    and abs(val_n - val_a) >= 1)
                    if saldo_cambio:
                        # Solo saltar el DOM si el saldo actual coincide con lo que
                        # el XHR anterior calculó. Si saldo_actual > saldo_en_ultimo_
                        # movimiento, llegó una tx adicional que el XHR no capturó
                        # (ej: $110 ó $537.900 inmediatamente después de otra tx) →
                        # en ese caso NO saltar para no perder el movimiento.
                        _val_ult = (_parsear_pesos(self.saldo_en_ultimo_movimiento)
                                    if self.saldo_en_ultimo_movimiento else None)
                        _xhr_ya_cubrio = (
                            self._ciclos_skip_dom > 0
                            and _val_ult is not None
                            and abs((val_n or 0) - _val_ult) < 1
                        )
                        if _xhr_ya_cubrio:
                            self._ciclos_skip_dom -= 1
                            print(f"[{self.nombre}] ⏭️  DOM fallback pospuesto "
                                  f"(saldo propagado tras XHR anterior)")
                        elif self._saldo_ya_notificado(val_n - val_a):
                            # El salto de saldo = montos que la API ya notificó
                            # cuando Detalles aún no se había actualizado.
                            print(f"[{self.nombre}] ⏭️  Cambio de saldo ya notificado por la API "
                                  f"(Δ {val_n - val_a:+,.0f}) — no se repite "
                                  f"(repaso de páginas {REPASO_PAGINAS_S}s)")
                            self.saldo_en_ultimo_movimiento = saldo_actual
                            self._ciclos_skip_dom = 0
                            self._repasar_paginas_hasta = time.time() + REPASO_PAGINAS_S
                        else:
                            if self._ciclos_skip_dom > 0:
                                # XHR previo no cubrió este cambio → resetear contador
                                self._ciclos_skip_dom = 0
                            print(f"[{self.nombre}] 🔄 Saldo cambió sin tx XHR → DOM")
                            n_dom, filas_dom = self._fallback_dom(
                                page, saldo_anterior or "—", saldo_actual)
                            if n_dom:
                                _txs_dom_xhr = self._drenar_queue()
                                if _txs_dom_xhr:
                                    _txs_dom_xhr = self._recopilar_txs_hoy(page, _txs_dom_xhr)
                                    _nuevas_dom_api = [
                                        t for t in self._detectar_nuevas(_txs_dom_xhr)
                                        if self._es_fecha_reciente(
                                            t.get("transactionDate",""), dias=3)
                                    ]
                                    if _nuevas_dom_api:
                                        print(f"[{self.nombre}] ✅ Tx confirmada via XHR post-DOM")
                                        _saldo_fin = self._notificar_tx_batch(
                                            _nuevas_dom_api,
                                            self.saldo_en_ultimo_movimiento
                                            or saldo_anterior or "—")
                                        self.saldo_en_ultimo_movimiento = _saldo_fin
                                        txs = _txs_dom_xhr
                                    else:
                                        print(f"[{self.nombre}] ⚠️ XHR post-DOM sin tx → DOM")
                                        for _fila in filas_dom:
                                            threading.Thread(
                                                target=self._notificar_dom_alerta,
                                                args=(_fila, saldo_anterior or "—", saldo_actual),
                                                daemon=True
                                            ).start()
                                        self.saldo_en_ultimo_movimiento = saldo_actual
                                else:
                                    print(f"[{self.nombre}] ⚠️ Sin XHR post-DOM → DOM")
                                    for _fila in filas_dom:
                                        threading.Thread(
                                            target=self._notificar_dom_alerta,
                                            args=(_fila, saldo_anterior or "—", saldo_actual),
                                            daemon=True
                                        ).start()
                                    self.saldo_en_ultimo_movimiento = saldo_actual
                            elif saldo_actual != self._ultimo_saldo_notificado:
                                delta = (val_n or 0) - (val_a or 0)
                                txs_fresh = self._drenar_queue()
                                if txs_fresh:
                                    nuevas_fresh = [
                                        t for t in self._detectar_nuevas(txs_fresh)
                                        if self._es_fecha_reciente(
                                            t.get("transactionDate",""), dias=3)
                                    ]
                                    if nuevas_fresh:
                                        print(f"[{self.nombre}] ✅ Tx en API post-fallback")
                                        _base_pf = (_parsear_pesos(
                                            self.saldo_en_ultimo_movimiento
                                            or saldo_anterior or "") or 0.0)
                                        _sc_pf = _base_pf
                                        _batch_pf: list = []
                                        for _tx_pf in reversed(nuevas_fresh):
                                            _m_pf = float(_tx_pf.get("amount", 0))
                                            _sa_pf = ("$ " + f"{int(round(_sc_pf)):,}"
                                                      .replace(",", "."))
                                            _sc_pf += _m_pf
                                            _sact_pf = ("$ " + f"{int(round(_sc_pf)):,}"
                                                        .replace(",", "."))
                                            _batch_pf.append((_tx_pf, _sa_pf, _sact_pf))
                                        def _send_pf(_b=_batch_pf):
                                            for _t, _sa, _sact in _b:
                                                self._notificar_tx(_t, _sa, _sact)
                                        threading.Thread(target=_send_pf, daemon=True).start()
                                        self.saldo_en_ultimo_movimiento = (
                                            "$ " + f"{int(round(_sc_pf)):,}".replace(",", ".")
                                        )
                                    else:
                                        txs_r, nuevas_r = self._reintentar_tx(
                                            page, delta, txs_fresh)
                                        if nuevas_r:
                                            saldo_final_r = self._notificar_tx_batch(
                                                nuevas_r,
                                                self.saldo_en_ultimo_movimiento
                                                or saldo_anterior or "—")
                                            self.saldo_en_ultimo_movimiento = saldo_final_r
                                        else:
                                            print(f"[{self.nombre}] ⚠️ Fingerprint colisionada "
                                                  f"→ delta (Δ={delta:+.0f})")
                                            threading.Thread(
                                                target=self._notificar_saldo_cambio_inferido,
                                                args=(txs_r, saldo_anterior or "—",
                                                      saldo_actual, delta),
                                                daemon=True
                                            ).start()
                                            self.saldo_en_ultimo_movimiento = saldo_actual
                                        txs_fresh = txs_r or txs_fresh
                                    txs = txs_fresh
                                else:
                                    txs_r2, nuevas_r2 = self._reintentar_tx(page, delta, txs)
                                    if nuevas_r2:
                                        saldo_final_r2 = self._notificar_tx_batch(
                                            nuevas_r2,
                                            self.saldo_en_ultimo_movimiento
                                            or saldo_anterior or "—")
                                        self.saldo_en_ultimo_movimiento = saldo_final_r2
                                    else:
                                        print(f"[{self.nombre}] ⚠️ Fingerprint colisionada "
                                              f"→ delta (Δ={delta:+.0f})")
                                        threading.Thread(
                                            target=self._notificar_saldo_cambio_inferido,
                                            args=(txs_r2, saldo_anterior or "—",
                                                  saldo_actual, delta),
                                            daemon=True
                                        ).start()
                                        self.saldo_en_ultimo_movimiento = saldo_actual
                                    txs = txs_r2
                                self._ultimo_saldo_notificado = saldo_actual

                # Actualizar estado
                self._last_snapshot = list(txs)
                self._ultima_lectura_ts = time.time()
                self._actualizar_conocidas(txs)
                self.saldo_actual   = saldo_actual
                self._guardar_cache()

                if primera_vez:
                    primera_vez = False
                    self._baseline_ok = True   # baseline construido en el loop
                    if not self.primera_data_event.is_set():
                        _hoy_str = time.strftime("%Y/%m/%d")
                        self.primera_data_txs  = [t for t in txs if t.get("transactionDate","") == _hoy_str]
                        self.primera_data_saldo = saldo_actual
                        self.primera_data_event.set()
                        # El envío a Pochonance en el startup (línea ~640) se hizo con
                        # txs_init vacío porque la API no respondió a tiempo — acá ya
                        # tenemos los datos reales del loop, así que se reenvía para que
                        # Cuentas P2P no se quede con "Sin movimientos recientes" mientras
                        # el bot de Telegram "Saldo" sí muestra el historial completo.
                        bridge_conexion_exitosa(self.nombre, saldo_actual, self.primera_data_txs)

                # GC proactivo cada 30 ciclos
                _ciclos_gc += 1
                _diag_ciclos += 1
                if _ciclos_gc >= GC_CADA_CICLOS:
                    _ciclos_gc = 0
                    try:
                        page.evaluate("() => { try { window.gc && window.gc(); } catch(e){} }")
                        print(f"[{self.nombre}] 🧹 GC preventivo OK")
                    except Exception:
                        print(f"[{self.nombre}] ⚠️ GC evaluate falló — ignorado")

                # Reinicio preventivo por RAM (solo cada 5 ciclos)
                if _ciclos_gc % 5 == 0:
                    _mem_mb  = self._memoria_sesion_mb()
                    _limite  = _ram_limite_mb()
                    _warn_mb = int(_limite * 0.70)

                    # Diagnóstico 🔬: qué se acumula dentro de Chrome con el
                    # tiempo (solo imprime; no cambia nada del monitoreo).
                    try:
                        _pm = page.evaluate("""() => ({
                            nodos:   document.getElementsByTagName('*').length,
                            heap:    performance.memory ? performance.memory.usedJSHeapSize : 0,
                            iframes: document.getElementsByTagName('iframe').length,
                        })""")
                        _n = max(_diag_ciclos, 1)
                        _top = ", ".join(
                            f"…{u[-40:]} ×{c}"
                            for u, c in self._diag_req_urls.most_common(3)
                        )
                        print(f"[{self.nombre}] 🔬 Diag: nodos DOM {_pm['nodos']:,}"
                              f" | JS heap {_pm['heap'] / 1048576:.0f} MB"
                              f" | iframes {_pm['iframes']}"
                              f" | RAM {_mem_mb:.0f} MB"
                              f" | req/ciclo {self._diag_req_total / _n:.0f}"
                              f" | top: {_top}")
                    except Exception:
                        pass
                    self._diag_req_total = 0
                    self._diag_req_urls  = Counter()
                    _diag_ciclos = 0

                    if _mem_mb >= _warn_mb:
                        _critico = _mem_mb >= _limite * 0.92
                        print(f"[{self.nombre}] 📊 RAM Chrome: {_mem_mb:.0f} MB"
                              f"{' ⚠️ CRÍTICO' if _critico else ' — acercándose al límite'}"
                              f" (límite: {_limite} MB | sesiones: {_sesiones_activas_count})")
                    if _mem_mb >= _limite:
                        raise Exception(
                            f"Reinicio preventivo de memoria ({_mem_mb:.0f} MB ≥ {_limite} MB)"
                        )

                time.sleep(self.INTERVALO_TOGGLE)

        finally:
            self._en_loop = False
            _liberar_sem()
            self._context    = None
            self._playwright = None
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if p is not None:
                try:
                    p.stop()
                except Exception:
                    pass
            self._matar_chrome_zombie()

    # ── Ciclo externo con reintentos ───────────────────────────────
    def run(self):
        threading.Thread(target=self._watchdog, daemon=True).start()
        _intentos_login_fallidos = 0
        _MAX_LOGIN_FALLOS = 8
        _fallos_arranque  = 0
        _MAX_ARRANQUE     = 5
        # Caídas forzadas por Bancolombia (error 999 sin botón de reintentar,
        # captcha, etc. — ver _sesion_completa/popup_info) que hasta ahora
        # NUNCA hacían morir el hilo: como cada una solo reconectaba y volvía
        # a intentar, is_alive() se quedaba en True para siempre y el
        # heartbeat seguía reportando la cuenta como activa aunque no
        # estuviera sirviendo nada (05/09/2026, a partir del caso real con
        # la cuenta "X"). Con este contador, tras varias seguidas se da
        # por muerta de verdad, para que Pochonance SÍ note la caída.
        _fallos_sesion_forzada = 0
        _MAX_SESION_FORZADA    = 5
        # Sesiones SEGUIDAS que entraron al banco pero NUNCA llegaron a leerlo porque no aparece el menú
        # ("No se encuentra el sidebar × 5"). Antes ese error reiniciaba todos los contadores y el ciclo
        # login → sin menú → reconectar se repetía sin fin (38 veces con una cuenta, 27/10).
        _fallos_sin_menu = 0
        _MAX_SIN_MENU    = 3
        while self.running:
            self._pausado.wait()
            if not self.running:
                break
            _t_sesion_ini = time.time()
            try:
                self._sesion_completa()
                # _sesion_completa retornó sin excepción → login fallido
                _desmarcar_arrancando(self.nombre)   # no bloquear a las demás mientras espera
                if not self.running:
                    break   # detenida (no fue un fallo de login)
                _fallos_arranque = 0
                _intentos_login_fallidos += 1
                self._fallos_login_seguidos = _intentos_login_fallidos   # prioridad de turno
                # Si el fallo fue por reCAPTCHA, el relanzamiento en
                # Chromium+CapSolver es DELIBERADO (no un fallo transitorio que
                # necesite enfriamiento): pausa corta en vez de 10s+.
                if getattr(self, "_captcha_requiere_chromium", False):
                    pausa_login = 2
                else:
                    pausa_login = min(10 * _intentos_login_fallidos, 60)
                print(f"[{self.nombre}] ⚠️ Login no completado "
                      f"(intento {_intentos_login_fallidos}/{_MAX_LOGIN_FALLOS}) "
                      f"— reintentando en {pausa_login}s")
                if _intentos_login_fallidos >= _MAX_LOGIN_FALLOS:
                    _safe_send(
                        OWNER_CHAT_ID,
                        f"🔴 *{self.nombre}* — {_MAX_LOGIN_FALLOS} fallos de login "
                        f"consecutivos. Detener y reiniciar manualmente.",
                        parse_mode="Markdown"
                    )
                    if not self.startup_done.is_set():
                        # Nunca llegó a entrar ni una vez desde que se activó —
                        # Pochonance necesita saber que la activación no funcionó
                        # (una cuenta que ya venía funcionando bien no reporta
                        # esto por una racha de fallos operativos posteriores).
                        bridge_error_login(
                            self.nombre,
                            self._motivo_login_fallido
                            or f"{_MAX_LOGIN_FALLOS} intentos de login fallidos"
                        )
                    self.running = False
                    break
                if self.running and self._pausado.is_set():
                    for _ in range(int(pausa_login)):
                        self._ultimo_latido = time.time()
                        if not self.running or not self._pausado.is_set():
                            break
                        time.sleep(1)
            except Exception as e:
                _intentos_login_fallidos = 0
                self._fallos_login_seguidos = 0
                err_str = str(e)
                print(f"[{self.nombre}] Sesión caída: {err_str[:120]}")
                if self.running and self._pausado.is_set():
                    _marcar_arrancando(self.nombre)   # reconectando: las demás esperan
                # Si ya estaba ciega (varias caídas seguidas) se conserva el
                # instante de la PRIMERA, para medir el hueco completo.
                if self._t_ciega_desde is None:
                    self._t_ciega_desde = time.time()
                    self._motivo_ciega  = err_str[:60]
                es_oom          = "out of memory" in err_str.lower() or "oom del renderer" in err_str.lower()
                es_crash        = any(k in err_str.lower() for k in (
                    "crashed","target closed","browsercontext","process","renderer"
                ))
                # Incluye "Reinicio preventivo por lentitud" (mismo tratamiento).
                es_reinicio_mem = "reinicio preventivo" in err_str.lower()
                es_sesion_forzada = "sesión cerrada por bancolombia" in err_str.lower()
                es_sin_menu = ("no se encuentra el sidebar" in err_str.lower()
                               or "sin 'tus cuentas'" in err_str.lower())
                if not es_sin_menu:
                    _fallos_sin_menu = 0
                pausa = 2 if es_reinicio_mem else (30 if es_oom else (5 if es_crash else 3))

                es_error_arranque = any(k in err_str.lower() for k in (
                    "profile","singleton","lock","failed to launch",
                    "executable doesn't exist","spawn","enoent","permission"
                ))
                if es_reinicio_mem:
                    _fallos_arranque = 0
                    _fallos_sesion_forzada = 0
                elif es_error_arranque or es_oom:
                    _fallos_arranque += 1
                    _fallos_sesion_forzada = 0
                    if _fallos_arranque >= _MAX_ARRANQUE:
                        print(f"[{self.nombre}] 🔴 {_MAX_ARRANQUE} fallos de arranque — deteniendo")
                        _safe_send(
                            OWNER_CHAT_ID,
                            f"🔴 *{self.nombre}* — {_MAX_ARRANQUE} fallos de arranque "
                            f"seguidos. Detener y reiniciar manualmente.",
                            parse_mode="Markdown"
                        )
                        self.running = False
                        break
                elif es_sesion_forzada:
                    _fallos_arranque = 0
                    _fallos_sesion_forzada += 1
                    if _fallos_sesion_forzada >= _MAX_SESION_FORZADA:
                        print(f"[{self.nombre}] 🔴 {_MAX_SESION_FORZADA} caídas de sesión forzadas "
                              f"por Bancolombia seguidas (posible bloqueo/captcha/error 999) — deteniendo")
                        _safe_send(
                            OWNER_CHAT_ID,
                            f"🔴 *{self.nombre}* — {_MAX_SESION_FORZADA} caídas de sesión forzadas "
                            f"por Bancolombia seguidas (posible bloqueo/captcha). Deteniendo el "
                            f"monitoreo — revisa manualmente.",
                            parse_mode="Markdown"
                        )
                        self.running = False
                        break
                elif es_sin_menu:
                    _fallos_arranque = 0
                    _fallos_sesion_forzada = 0
                    # Solo cuenta si esta sesión NO llegó a leer el banco ni una vez.
                    if getattr(self, "_ultima_lectura_ts", 0) >= _t_sesion_ini:
                        _fallos_sin_menu = 0
                    else:
                        _fallos_sin_menu += 1
                    if _fallos_sin_menu >= _MAX_SIN_MENU:
                        print(f"[{self.nombre}] 🔴 {_MAX_SIN_MENU} entradas seguidas sin ver el menú de Bancolombia "
                              f"— se DETIENE esta cuenta (no se sigue intentando)")
                        COOLDOWN_CUENTAS[self.nombre] = time.time() + ENFRIAMIENTO_SIN_MENU_S
                        _safe_send(
                            OWNER_CHAT_ID,
                            f"🔴 {self.nombre} — {_MAX_SIN_MENU} entradas seguidas a Bancolombia sin ver el menú "
                            f"(pantalla rara o bloqueo). Se detiene y no se reintenta en "
                            f"{ENFRIAMIENTO_SIN_MENU_S // 60} min. Revisa el último incidente 'sin_menu'."
                        )
                        if not self.startup_done.is_set():
                            bridge_error_login(self.nombre, "Bancolombia no mostró el menú tras iniciar sesión "
                                                            "(pantalla rara o bloqueo temporal). No se reintenta ahora.")
                        self.running = False
                        break
                else:
                    _fallos_arranque = 0
                    _fallos_sesion_forzada = 0

                # Errores de sesión van solo a la terminal (no al chat Telegram)
                if self.running:
                    if self._pausado.is_set():
                        print(f"[{self.nombre}] Reconectando en {pausa}s...")
                        for _ in range(int(pausa)):
                            self._ultimo_latido = time.time()
                            if not self.running or not self._pausado.is_set():
                                break
                            time.sleep(1)
                    else:
                        self._pausado.wait()

        _desmarcar_arrancando(self.nombre)   # hilo terminado
        # Garantía final: señalar startup fallido si nunca lo hicimos
        if not self.startup_done.is_set():
            self.startup_ok = False
            self.startup_done.set()

    def _saldo_ya_notificado(self, delta: float, vigencia_s: float = 90) -> bool:
        """
        True si `delta` (salto de saldo sin tx nueva en la API) coincide con 1-3
        montos que la API notificó antes de que el saldo se moviera (en los
        últimos `vigencia_s` s). Los montos usados se consumen: cada uno explica
        un solo salto, así dos txs reales iguales no se tapan entre sí.
        """
        from itertools import combinations
        ahora = time.time()
        self._xhr_sin_saldo = [(t, m) for t, m in self._xhr_sin_saldo
                               if ahora - t < vigencia_s]
        pend = self._xhr_sin_saldo
        for k in range(1, min(3, len(pend)) + 1):
            for idx in combinations(range(len(pend)), k):
                if abs(sum(pend[i][1] for i in idx) - delta) < 1:
                    self._xhr_sin_saldo = [p for i, p in enumerate(pend) if i not in idx]
                    return True
        return False

    def detener_suave(self, max_s: float = 20):
        """
        Detener dejando terminar el ciclo en curso (máx. max_s segundos), para
        que lo que la cuenta ya vio se notifique antes de cerrar. Caso real
        29/09 19:05:03: Una cuenta vio salir $2.490.000 y Pochonance la detuvo
        ese mismo segundo → el movimiento no se notificó. Si la cuenta no está
        en el loop de monitoreo (arrancando/reconectando) se detiene de una.
        Bloquea hasta max_s: llamarla desde un hilo aparte.
        """
        if self._en_loop and self.running:
            self._detener_pedido = True
            limite = time.time() + max_s
            while self._en_loop and self.is_alive() and time.time() < limite:
                time.sleep(0.5)
        self.stop()

    def stop(self):
        self.running = False
        _desmarcar_arrancando(self.nombre)
        self._pausado.set()
        self._renderer_muerto.set()   # dispara excepción en el loop de monitoreo
        # ── NUNCA llamar ctx.close()/pw.stop() desde aquí ────────────────
        # Playwright usa greenlets internamente. Llamarlo desde un hilo externo
        # (el handler de Telegram, el loop de reiniciar, etc.) lanza
        # greenlet.error, corrompiendo el estado interno y dejando Chrome en
        # estado inconsistente: la siguiente sesión abre Chrome pero la página
        # queda en blanco o crashea de inmediato (reproducido el 27/08/2026).
        #
        # Solución: stop() solo señaliza. El MonitorThread detecta
        # _renderer_muerto, lanza su excepción y en su PROPIO hilo hace el
        # cleanup de Playwright (context.close, pw.stop, _matar_chrome_zombie)
        # desde el bloque finally de _sesion_completa — el único lugar seguro.
        # ─────────────────────────────────────────────────────────────────────
        self._context    = None   # evitar double-close en el finally de _sesion_completa
        self._playwright = None
