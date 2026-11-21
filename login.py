# -*- coding: utf-8 -*-
# =====================================================================
# LOGIN.PY
# Responsabilidad: acceso a credenciales y apertura de sesión.
# - Bitwarden: obtener sesión, credenciales y lista de cuentas
# - LoginMixin: _login(), navegación de startup, aceptar documentos,
#               _parchar_prefs(), teclado de selección de cuentas
# =====================================================================
import os
import sys
import json
import time
import subprocess
import threading
import shutil
import re
import unicodedata

import telebot

from notificar import bot, _enviar, OWNER_CHAT_ID, pausa_humana, _es_fatal

from patchright.sync_api import TimeoutError as PlaywrightTimeoutError   # Nodo_B_Patchright (la clase de error debe ser la de Patchright)

# Toda la resolución de reCAPTCHA vive SOLO en capsolver.py — login.py nada
# más la USA. En Edge (sin extensión) se señaliza para relanzar con
# Chromium+CapSolver; en Chromium con extensión se espera a que ella clive el
# checkbox (la API queda como respaldo). No hay lógica de captcha acá.
from capsolver import (
    _detectar_recaptcha,
    _esperar_captcha_resuelto_en_pagina,
    _resolver_captcha_con_capsolver,
    _extension_capsolver_cargada,
    _diagnosticar_captcha,
    HOOK_GRECAPTCHA_JS,
)

# Señal para forzar relanzamiento con Chromium + CapSolver extension
SEÑAL_CAPTCHA_REQUIERE_CHROMIUM = "__CAPTCHA_REQUIERE_CHROMIUM__"

# ── Rutas globales ─────────────────────────────────────────────────
LAST_CHAT_FILE = os.path.expanduser(
    "~/Documents/AutomatizacionBancolombia/ultimo_chat.json"
)

# ── Credenciales desde config.py (no sube a git) ──────────────────
from config import BW_CLIENT_ID, BW_CLIENT_SECRET, BW_MASTER_PASS

# ── URLs Bancolombia ───────────────────────────────────────────────
URL_BANCO    = "https://svpersonas.apps.bancolombia.com/autenticacion"

def _encontrar_bw() -> str:
    found = shutil.which("bw")
    if found:
        return found
    if sys.platform == "win32":
        try:
            import winreg
            partes = []
            for hive, clave in [
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                (winreg.HKEY_CURRENT_USER, r"Environment"),
            ]:
                try:
                    with winreg.OpenKey(hive, clave) as k:
                        val, _ = winreg.QueryValueEx(
                            k, "Path" if hive == winreg.HKEY_LOCAL_MACHINE else "PATH"
                        )
                        partes.append(val)
                except Exception:
                    pass
            found = shutil.which("bw", path=";".join(partes))
            if found:
                return found
        except Exception:
            pass
    return "bw"

BW_EXE = _encontrar_bw()

_bw_session_cache = {"token": None}
_bw_lock = threading.Lock()

# ── Caché de ítems BW ──────────────────────────────────────────────
# Se llena en listar_cuentas_bw() y lo usa obtener_credenciales().
# Evita llamar bw list items por cada cuenta al arrancar el monitoreo
# (antes: N cuentas × ~5 s = 60+ s; ahora: búsqueda local instantánea).
_bw_items_cache: list = []

def _norm(s: str) -> str:
    """Normaliza Unicode, minúsculas y le QUITA los acentos/tildes para
    comparaciones robustas.

    Incidente 08/09/2026: el ítem de Bitwarden se llamaba "Marta Beltran"
    (sin tilde) pero Cuentas P2P pide la cuenta como "Ana Marta Beltrán"
    (con tilde en la "ó"). Antes _norm() solo hacía NFC + lower, así que para
    la comparación "beltran" y "beltrán" eran palabras DISTINTAS y la
    coincidencia correcta y precisa nunca se daba — el código terminaba
    cayendo a una búsqueda más floja en Bitwarden que trajo varios resultados
    parecidos, y de ahí (ver _buscar_en_items) se terminó usando la cuenta de
    otra persona ("Ana Peñalosa"). Quitar los acentos aquí (con NFKD, que
    separa la letra de su tilde, y luego descartando las marcas combinantes)
    hace que "Beltrán" y "Beltran" se reconozcan como la misma palabra.
    """
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))

def _match_flexible(nombre_bw: str, buscado: str) -> bool:
    """
    True si nombre_bw y buscado corresponden al mismo ítem.
    Niveles probados en orden:
      1. Exacto:                "Ana Peñalosa"  == "Ana Peñalosa"
      2. Buscado ⊆ BW:         "Marta"  in  "Ana Marta Beltran"
      3. BW ⊆ Buscado:         "Ana"     in  "Ana Peñalosa"
      4. Palabras BW ⊆ buscado: todas las palabras del ítem BW aparecen en buscado
      5. Palabras buscado ⊆ BW: todas las palabras buscadas aparecen en el ítem BW
    """
    nb = _norm(nombre_bw)
    bu = _norm(buscado)
    if nb == bu:                                         return True
    if bu in nb:                                         return True
    if nb in bu:                                         return True
    pb, pu = set(nb.split()), set(bu.split())
    if pb and pb <= pu:                                  return True
    if pu and pu <= pb:                                  return True
    return False

def _nivel_match(nombre_bw: str, buscado: str) -> int:
    """Qué tan preciso es el emparejamiento, de más a menos confiable
    (0 = no coincide). Se usa para que, entre varios ítems que coinciden,
    siempre gane el más preciso — antes _buscar_en_items se quedaba con el
    PRIMERO que topara cualquier nivel (hasta el más flojo), así que el orden
    en que Bitwarden devolvía los ítems podía decidir qué cuenta real se
    abría, en vez de cuál era realmente la correcta."""
    nb = _norm(nombre_bw)
    bu = _norm(buscado)
    if nb == bu:              return 5
    if bu in nb:               return 4
    if nb in bu:               return 3
    pb, pu = set(nb.split()), set(bu.split())
    if pb and pb <= pu:        return 2
    if pu and pu <= pb:        return 1
    return 0

def _buscar_en_items(items: list, nombre_item: str):
    """Devuelve el ítem BW que MEJOR coincide con nombre_item (el de nivel
    más alto entre TODOS los ítems, no el primero que topa cualquier nivel),
    o None si ninguno coincide de verdad."""
    mejor, mejor_nivel = None, 0
    for i in items:
        nivel = _nivel_match(i.get("name", ""), nombre_item)
        if nivel > mejor_nivel:
            mejor, mejor_nivel = i, nivel
    return mejor

# ── Funciones Bitwarden ────────────────────────────────────────────
def _ejecutar_bw(args: list, session=None):
    env = os.environ.copy()
    env["BW_CLIENTID"]     = BW_CLIENT_ID
    env["BW_CLIENTSECRET"] = BW_CLIENT_SECRET
    env["BW_PASSWORD"]     = BW_MASTER_PASS
    env["NODE_OPTIONS"]    = "--max-old-space-size=512"
    if session:
        env["BW_SESSION"] = session
    return subprocess.run(
        args, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=env
    )

def obtener_sesion_bw() -> str | None:
    with _bw_lock:
        if _bw_session_cache["token"]:
            try:
                r = _ejecutar_bw([BW_EXE, "status"], session=_bw_session_cache["token"])
                if json.loads(r.stdout).get("status") == "unlocked":
                    return _bw_session_cache["token"]
            except Exception:
                pass
            _bw_session_cache["token"] = None
        try:
            try:
                r_st = _ejecutar_bw([BW_EXE, "status"])
                status_actual = json.loads(r_st.stdout).get("status", "unauthenticated")
            except Exception:
                status_actual = "unauthenticated"
            if status_actual != "unauthenticated":
                _ejecutar_bw([BW_EXE, "logout"])
                time.sleep(1)
            r_login = _ejecutar_bw([BW_EXE, "login", "--apikey"])
            if r_login.returncode != 0:
                return None
            token = None
            env_bw = {**os.environ.copy(),
                      "BW_CLIENTID": BW_CLIENT_ID,
                      "BW_CLIENTSECRET": BW_CLIENT_SECRET,
                      "BW_PASSWORD": BW_MASTER_PASS}
            try:
                r_u = subprocess.run(
                    [BW_EXE, "unlock", "--raw"],
                    input=BW_MASTER_PASS + "\n",
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30, env=env_bw,
                )
                if r_u.returncode == 0 and r_u.stdout.strip():
                    token = r_u.stdout.strip()
            except Exception:
                pass
            if not token:
                try:
                    r_u2 = _ejecutar_bw(
                        [BW_EXE, "unlock", "--passwordenv", "BW_PASSWORD", "--raw"]
                    )
                    if r_u2.returncode == 0 and r_u2.stdout.strip():
                        token = r_u2.stdout.strip()
                except Exception:
                    pass
            if not token:
                return None
            _bw_session_cache["token"] = token
            return token
        except Exception:
            return None

def obtener_credenciales(nombre_item: str):
    global _bw_items_cache

    # ── Ruta rápida: caché ya cargado por listar_cuentas_bw() ─────────
    if _bw_items_cache:
        item_exacto = _buscar_en_items(_bw_items_cache, nombre_item)
        if item_exacto:
            u = item_exacto.get("login", {}).get("username", "")
            p = item_exacto.get("login", {}).get("password", "")
            if u and p:
                nb = item_exacto.get("name", "")
                if _norm(nb) != _norm(nombre_item):
                    print(f"[BW] Credenciales OK (caché): '{nb}' → buscado: '{nombre_item}'")
                else:
                    print(f"[BW] Credenciales OK (caché): '{nb}'")
                return u, p

    # ── Ruta lenta: consultar bw directamente ─────────────────────────
    sesion = obtener_sesion_bw()
    if not sesion:
        return None, None

    def _bw_search(termino: str):
        """bw list items --search <termino> -> lista de items, o [] si falla,
        no responde, o el JSON es inválido (nunca lanza)."""
        r = _ejecutar_bw([BW_EXE, "list", "items", "--search", termino], session=sesion)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        try:
            return json.loads(r.stdout) or []
        except json.JSONDecodeError:
            return []

    # Búsqueda primaria con el nombre completo
    items = _bw_search(nombre_item)
    # Bitwarden exige que el ítem CONTENGA la frase buscada: si el nombre
    # completo (ej. "Eva Sofía Cardona") tiene más palabras que el ítem
    # guardado (ej. "Eva"), la búsqueda de Bitwarden devuelve [] -- una
    # respuesta VÁLIDA sin resultados, no un error. Antes solo se reintentaba
    # con la primera palabra cuando bw fallaba o no devolvía texto; una lista
    # vacía "exitosa" no disparaba el reintento, así que estos casos siempre
    # fallaban con "Sin credenciales en Bitwarden" aunque el ítem sí existiera
    # (bug confirmado el 31/08/2026 reproduciendo el `bw list items --search`
    # a mano). Ahora también se reintenta cuando la búsqueda fue válida pero
    # no trajo nada — mismo criterio que ya usaba _match_flexible más abajo,
    # solo que aplicado también a la búsqueda que le manda a Bitwarden.
    if not items:
        primera = nombre_item.strip().split()[0] if nombre_item.strip() else nombre_item
        items = _bw_search(primera)
    if not items:
        return None, None

    # Incidente 08/09/2026 (Ana Marta Beltrán → abrió la cuenta de "Ana
    # Peñalosa"): antes, si _buscar_en_items() no encontraba ninguna
    # coincidencia confiable, el " or items[0]" tomaba CIEGAMENTE el primer
    # resultado que Bitwarden hubiera devuelto para la búsqueda floja, sin
    # verificar que fuera la persona correcta. Con varias cuentas de nombres
    # parecidos eso puede abrir la sesión bancaria de otra persona sin
    # ningún aviso. Ahora, si no hay una coincidencia confiable, se falla
    # explícitamente (None, None) en vez de adivinar.
    item_exacto = _buscar_en_items(items, nombre_item)
    if not item_exacto:
        # DIAGNÓSTICO TEMPORAL (08/09/2026, incidente Ana Marta Beltrán) —
        # imprime el nombre crudo (con repr() para ver espacios/caracteres
        # invisibles) de cada candidato y su forma normalizada, para ver
        # EXACTAMENTE por qué no calza contra nombre_item. Quitar una vez
        # resuelto.
        nombres_crudos = [repr(i.get("name", "")) for i in items]
        print(f"[BW][DEBUG] buscado={_norm(nombre_item)!r} nombre_item={nombre_item!r}")
        for i in items:
            print(f"[BW][DEBUG] candidato name={i.get('name','')!r} normalizado={_norm(i.get('name',''))!r} nivel={_nivel_match(i.get('name',''), nombre_item)}")
        print(f"[BW] Sin coincidencia confiable para '{nombre_item}' "
              f"(Bitwarden devolvió {len(items)} resultado(s) parecido(s): {nombres_crudos}, "
              f"ninguno pasó la validación) — no se adivina, se aborta.")
        return None, None
    usuario    = item_exacto.get("login", {}).get("username", "")
    contrasena = item_exacto.get("login", {}).get("password", "")
    if not usuario or not contrasena:
        return None, None
    nombre_bw = item_exacto.get("name", "")
    if _norm(nombre_bw) != _norm(nombre_item):
        print(f"[BW] Credenciales OK: '{nombre_bw}' (buscado: '{nombre_item}')")
    else:
        print(f"[BW] Credenciales OK: '{nombre_bw}'")
    return usuario, contrasena

def listar_cuentas_bw(forzar: bool = False) -> tuple:
    # 'forzar' lo manda el botón "🔃 Actualizar cuentas" del menú (iniciar.py)
    # después de vaciar _bw_items_cache a mano. Esta función siempre vuelve a
    # consultar Bitwarden igual (el parámetro no cambia su comportamiento),
    # pero antes NI SIQUIERA lo aceptaba — listar_cuentas_bw(forzar=True)
    # lanzaba TypeError dentro del hilo del botón (sin ningún aviso visible,
    # el hilo simplemente moría), así que "Actualizar cuentas" nunca
    # funcionó y la única forma real de refrescar la caché era reiniciar
    # todo el proceso (confirmado el 08/09/2026: el ítem de Bitwarden se
    # renombró para agregar la tilde y el bot, ya corriendo, nunca se enteró).
    global _bw_items_cache
    sesion = obtener_sesion_bw()
    if not sesion:
        return [], "No se pudo autenticar con Bitwarden"
    try:
        _ejecutar_bw([BW_EXE, "sync"], session=sesion)
    except Exception:
        pass
    resultado = _ejecutar_bw([BW_EXE, "list", "items"], session=sesion)
    if resultado.returncode != 0:
        err = (resultado.stderr or resultado.stdout or "error").strip()[:200]
        return [], f"bw list items falló: {err}"
    try:
        items = json.loads(resultado.stdout)
    except Exception as e:
        return [], f"JSON inválido: {e}"
    if not items:
        return [], "Vault vacío"
    cuentas_login = [
        i for i in items
        if i.get("type") == 1
        and i.get("login", {}).get("username")
        and i.get("login", {}).get("password")
    ]
    # ── Llenar caché de ítems ─────────────────────────────────────────
    # obtener_credenciales() lo usa para evitar llamadas adicionales a bw.
    _bw_items_cache = cuentas_login
    cuentas = [{"name": i["name"]} for i in cuentas_login]
    return cuentas, None

# ── Teclado de selección (para iniciar.py) ─────────────────────────
def _teclado_seleccion(cuentas: list, seleccionadas: set, ya_activas: set = None):
    ya_activas = ya_activas or set()
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for c in cuentas:
        nombre = c["name"]
        if nombre in ya_activas:
            markup.add(telebot.types.InlineKeyboardButton(
                f"🟢  {nombre}  (activa)",
                callback_data=f"mon_activa_noop:{nombre}"
            ))
        else:
            tick = "✅" if nombre in seleccionadas else "◻️"
            markup.add(telebot.types.InlineKeyboardButton(
                f"{tick}  {nombre}", callback_data=f"mon_toggle:{nombre}"
            ))
    markup.row(
        telebot.types.InlineKeyboardButton("▶️  Comenzar monitoreo", callback_data="mon_comenzar"),
        telebot.types.InlineKeyboardButton("❌  Cancelar",            callback_data="mon_cancelar"),
    )
    return markup

# ══════════════════════════════════════════════════════════════════
# LoginMixin
# Métodos de inicio de sesión y navegación de startup.
# Se mezcla en MonitorThread vía herencia múltiple (sesiones.py).
# ══════════════════════════════════════════════════════════════════
# ── Bloqueo temporal de inicio de sesión (27/10/2026) ─────────────────────────────────────────
# Bancolombia a veces responde al login con /error-page: "Debes esperar para volver a intentarlo.
# Por seguridad, en este momento no puedes iniciar sesión" (bloqueo de la IP del PC). El login
# parecía salir bien (la URL ya no es /autenticacion) y el sistema reintentaba sin parar. Ahora
# eso cuenta como login FALLIDO, se avisa UNA vez al dueño y todo este PC deja de intentar entrar
# durante _ESPERA_BLOQUEO_MIN minutos (cada intento nuevo puede alargar el bloqueo).
_ESPERA_BLOQUEO_MIN = 15
_bloqueo = {"hasta": 0.0, "aviso": 0.0, "n": 0}   # n = veces SEGUIDAS que el banco pidió esperar
_bloqueo_lock = threading.Lock()
_PATRON_ESPERA = re.compile(r"debes esperar para volver a intentarlo|no puedes iniciar sesi", re.IGNORECASE)


class LoginMixin:

    def _diag_login_fallido(self, page, paso: str, error=None):
        """Solo diagnóstico (no cambia el flujo): imprime qué muestra la página cuando el
        login no encuentra el campo de usuario y guarda un incidente con captura
        (ver incidentes.py). Se llama ANTES de escribir credenciales, así la captura no
        las muestra. innerText no incluye lo escrito en los campos."""
        try:
            info = page.locator("html").evaluate(
                """(raiz) => ({
                    url: location.href, titulo: document.title, estado: document.readyState,
                    inputs: document.querySelectorAll('input').length,
                    txt: document.body ? document.body.innerText : '',
                    btns: [...document.querySelectorAll('button, a[role="button"]')]
                          .filter(b => { const r = b.getBoundingClientRect();
                                         return r.width > 0 && r.height > 0; })
                          .map(b => (b.textContent || b.getAttribute('aria-label') || '').trim())
                          .filter(t => t).slice(0, 8)
                })""", timeout=3000)
            txt = " ".join((info.get("txt") or "").split())[:300]
            print(f"[{self.nombre}] 🔎 Sin campo de usuario — {info.get('url')} | estado={info.get('estado')} "
                  f"| inputs={info.get('inputs')} | página dice: '{txt}' | botones: {info.get('btns')}")
        except Exception as _e:
            print(f"[{self.nombre}] 🔎 Sin campo de usuario — no se pudo leer la página ({_e})")
        # La pantalla de bloqueo del WAF de Bancolombia (Imperva, "Access denied / Error 15")
        # llega dentro de un iframe: el texto de la página principal sale vacío. Se lee el de
        # cada marco. Si es ese bloqueo, no tiene sentido reintentar y machacar el WAF (cada
        # intento puede alargar el bloqueo): se deja el motivo para que sesiones.py avise a
        # Pochonance y detenga la cuenta, como ya hace con otros bloqueos explícitos.
        try:
            for _marco in page.frames:
                try:
                    _t = " ".join((_marco.locator("html").evaluate(
                        "(r) => document.body ? document.body.innerText : ''", timeout=2000) or "").split())
                except Exception:
                    continue
                if re.search(r"access denied|error\s*15|request was blocked|imperva|incapsula", _t, re.I):
                    print(f"[{self.nombre}] 🚫 Bancolombia BLOQUEÓ el acceso desde la IP de este PC "
                          f"(WAF Imperva): '{_t[:160]}'")
                    if not getattr(self, "_motivo_login_fallido", None):
                        self._motivo_login_fallido = ("Bancolombia bloqueó el acceso desde la IP de este PC "
                                                      "(Imperva, Error 15). No se reintenta.")
                    break
        except Exception:
            pass
        _red = list(getattr(self, "_login_red", []) or [])
        print(f"[{self.nombre}] 🔎 Red durante el login: "
              + (" | ".join(_red[:12]) if _red else "sin fallos ni respuestas >= 400"))
        try:
            import incidentes
            incidentes.registrar(self, page, "login_fallido", paso=paso, error=error)
        except Exception:
            pass

    def _login(self, page) -> bool:
        """Envuelve al login de siempre (_login_interno) solo para ANOTAR, mientras dura el
        login, las peticiones que fallan, las respuestas HTTP >= 400 y los errores de la
        consola de la página. Se muestran únicamente si falta el campo de usuario (ver
        _diag_login_fallido). No cambia el flujo; los oyentes se quitan al terminar para no
        recargar la CPU durante el monitoreo."""
        anotaciones: list = []
        self._login_red = anotaciones

        def _corta(u: str) -> str:
            return (u or "").split("?")[0][:100]

        def _al_fallar(req):
            try:
                if len(anotaciones) < 25:
                    anotaciones.append(f"FALLÓ {req.method} {_corta(req.url)} ({(req.failure or '')[:60]})")
            except Exception:
                pass

        def _al_responder(resp):
            try:
                if resp.status >= 400 and len(anotaciones) < 25:
                    anotaciones.append(f"HTTP {resp.status} {_corta(resp.url)}")
            except Exception:
                pass

        def _al_consola(msg):
            try:
                if msg.type == "error" and len(anotaciones) < 25:
                    anotaciones.append(f"consola: {(msg.text or '')[:100]}")
            except Exception:
                pass

        try:
            page.on("requestfailed", _al_fallar)
            page.on("response", _al_responder)
            page.on("console", _al_consola)
        except Exception:
            pass
        try:
            return self._login_interno(page)
        finally:
            for _ev, _fn in (("requestfailed", _al_fallar), ("response", _al_responder),
                             ("console", _al_consola)):
                try:
                    page.remove_listener(_ev, _fn)
                except Exception:
                    pass

    def _detectar_pagina_espera(self, page) -> bool:
        """¿La página es la de "Debes esperar para volver a intentarlo" de Bancolombia?"""
        try:
            if "error-page" in page.url:
                return True
        except Exception:
            pass
        try:
            txt = page.locator("html").evaluate(
                "(r) => document.body ? document.body.innerText : ''", timeout=2000) or ""
            return bool(_PATRON_ESPERA.search(txt))
        except Exception:
            return False

    def _login_interno(self, page) -> bool:
        """
        Navega a la URL de login, escribe credenciales y confirma.
        Retorna True si el login fue exitoso, False si falló.
        El caller (run()) trata False como retorno normal y reintenta.
        """
        try:
            page.add_init_script(HOOK_GRECAPTCHA_JS)
        except Exception:
            pass
        resto = _bloqueo["hasta"] - time.time()
        if resto > 0:
            # Otro login de este PC ya recibió el aviso de espera: no se toca el banco.
            if time.time() - getattr(self, "_t_aviso_bloqueo", 0) > 60:
                self._t_aviso_bloqueo = time.time()
                print(f"[{self.nombre}] ⏸️ Bancolombia pidió esperar (bloqueo temporal del inicio de sesión) "
                      f"— no se intenta entrar; faltan ~{resto / 60:.0f} min")
            return False
        page.goto(URL_BANCO, wait_until="domcontentloaded")
        pausa_humana(300, 600)
        try:
            campo = page.locator("input").nth(0)
            campo.wait_for(state="visible", timeout=22000)
            campo.click(click_count=3)
            campo.press_sequentially(self.usuario, delay=55)
            pausa_humana(100, 200)
        except Exception as e:
            print(f"[{self.nombre}] Error campo usuario: {e}")
            self._diag_login_fallido(page, "campo_usuario", e)
            return False
        try:
            campo = page.locator("input").nth(1)
            campo.click(click_count=3)
            campo.press_sequentially(self.contrasena, delay=55)
            pausa_humana(100, 200)
        except Exception as e:
            print(f"[{self.nombre}] Error campo clave: {e}"); return False
        # --- Re-envío con captcha (utilidad ÚNICA) --------------------------
        # Bancolombia BORRA usuario y clave cuando suelta el reCAPTCHA. El orden
        # real que funciona a mano es: re-escribir credenciales → resolver el
        # captcha → recién entonces clic "Iniciar sesión". Se usa tras el clic
        # inicial y durante la espera del loop. Devuelve True si el captcha
        # quedó resuelto y se re-envió el login.
        _reenvios_cred = 0

        def _reenviar_con_captcha(motivo: str, sitekey=None, timeout_seg: int = 60) -> bool:
            _con_ext_local = getattr(self, "_capsolver_extension", False)
            # 1) ¿Hay captcha?
            hay_captcha = sitekey is not None
            if not hay_captcha:
                try:
                    hay_captcha, _det_sk = _detectar_recaptcha(page, timeout_ms=1000)
                except Exception:
                    hay_captcha, _det_sk = False, None
                if not sitekey:
                    sitekey = _det_sk
            # SIEMPRE: en carril SIN extensión NO resolver por API -> ir a Chromium + extensión
            if not _con_ext_local and hay_captcha:
                print(f"[{self.nombre}] 🧩 {motivo} en Edge sin extensión → cerrando e intentando con Chromium + CapSolver extension...")
                try:
                    self._captcha_requiere_chromium = True
                except Exception:
                    pass
                try:
                    self._motivo_login_fallido = SEÑAL_CAPTCHA_REQUIERE_CHROMIUM
                except Exception:
                    pass
                return False
            if _con_ext_local:
                print(f"[{self.nombre}] 🧩 {motivo}; re-escribo credenciales "
                      f"({'ANTES de resolver el captcha' if hay_captcha else 'no hay captcha visible'}) "
                      f"y re-envío el login...")
            else:
                print(f"[{self.nombre}] 🧩 {motivo}; resuelvo el captcha, re-escribo "
                      f"credenciales y re-envío el login...")
            # 2) En carril con extensión: ingresar usuario/contraseña ANTES de resolver el captcha
            if _con_ext_local and hay_captcha:
                try:
                    page.locator("input").nth(0).click()
                    page.locator("input").nth(0).fill("")
                    page.locator("input").nth(0).type(self.usuario, delay=55)
                    pausa_humana(300, 600)
                    page.locator("input").nth(1).click()
                    page.locator("input").nth(1).fill("")
                    page.locator("input").nth(1).type(self.contrasena, delay=55)
                    pausa_humana(400, 800)
                    print(f"[{self.nombre}] ✅ Credenciales re-escritas ANTES de resolver el captcha (extensión).")
                except Exception as e:
                    print(f"[{self.nombre}] No pude re-escribir las credenciales antes del captcha: {e}")
            # 3) Resolverlo
            if hay_captcha:
                resolved = False
                if _con_ext_local:
                    try:
                        page.frame_locator(
                            "iframe[src*='recaptcha/api2/anchor']"
                        ).locator("#recaptcha-anchor").click(timeout=5000)
                        print(f"[{self.nombre}] 🧩 Checkbox del captcha clicado; "
                              f"espero a que la extensión lo resuelva...")
                    except Exception as e:
                        print(f"[{self.nombre}] ⚠️ No pude clicar el checkbox del captcha: {e}")
                        try:
                            _diagnosticar_captcha(page, self.nombre)
                        except Exception:
                            pass
                    # Espera CON PROGRESO: antes se llamaba de una sola vez y podía
                    # pasar hasta `timeout_seg` segundos sin imprimir nada (en el log
                    # parecía que el sistema se había colgado). Ahora se parte en
                    # tramos de 5 s, se informa cada 10 s y se aborta si el banco ya
                    # respondió con la página de "Debes esperar".
                    _t_esp = time.time()
                    _prox_aviso = _t_esp + 10
                    while time.time() - _t_esp < timeout_seg:
                        if _esperar_captcha_resuelto_en_pagina(page, timeout_seg=5):
                            resolved = True
                            break
                        try:
                            if self._detectar_pagina_espera(page):
                                print(f"[{self.nombre}] ⏳ Bancolombia pidió esperar mientras "
                                      f"se resolvía el captcha — dejo de reenviar el login.")
                                return False
                        except Exception:
                            pass
                        if time.time() >= _prox_aviso:
                            _prox_aviso = time.time() + 10
                            print(f"[{self.nombre}] ⏳ Esperando el token del captcha... "
                                  f"{time.time() - _t_esp:.0f}/{timeout_seg}s")
                    if resolved:
                        print(f"[{self.nombre}] ✅ Captcha resuelto en "
                              f"{time.time() - _t_esp:.0f}s (extensión/API).")
                    if not resolved and sitekey:
                        print(f"[{self.nombre}] 🧩 La extensión no lo resolvió en {timeout_seg}s; "
                              f"lo intento por la API de CapSolver...")
                        resolved = _resolver_captcha_con_capsolver(page, sitekey, self.nombre)
                else:
                    # Sin extensión (Firefox/Edge): resolver por API directamente
                    if sitekey:
                        resolved = _resolver_captcha_con_capsolver(page, sitekey, self.nombre)
                    else:
                        print(f"[{self.nombre}] ⚠️ CAPTCHA visible pero no pude extraer el sitekey para resolver por API.")
                if not resolved:
                    print(f"[{self.nombre}] ❌ No se pudo resolver el captcha; "
                          f"no reenvío el login con las credenciales.")
                    return False
            # 3b) Si NO había captcha visible pero sí vinimos por motivo de captcha y no estamos en extensión,
            #     mantener comportamiento anterior (re-escribir por si las borraron)
            if hay_captcha is False and not _con_ext_local:
                try:
                    page.locator("input").nth(0).click(); page.locator("input").nth(0).fill("")
                    page.locator("input").nth(0).type(self.usuario, delay=55)
                    pausa_humana(300, 600)
                    page.locator("input").nth(1).click(); page.locator("input").nth(1).fill("")
                    page.locator("input").nth(1).type(self.contrasena, delay=55)
                    pausa_humana(400, 800)
                except Exception as e:
                    print(f"[{self.nombre}] No pude re-escribir las credenciales: {e}")
            elif hay_captcha is False and _con_ext_local:
                # En extensión, ya intentamos re-escribir arriba si había captcha; si no lo había detectado
                # pero venimos por reenvío, asegurar credenciales presentes
                try:
                    v0 = page.locator("input").nth(0).input_value(timeout=500) or ""
                    v1 = page.locator("input").nth(1).input_value(timeout=500) or ""
                    if v0 == "" or v1 == "":
                        page.locator("input").nth(0).click(); page.locator("input").nth(0).fill("")
                        page.locator("input").nth(0).type(self.usuario, delay=55)
                        pausa_humana(200, 400)
                        page.locator("input").nth(1).click(); page.locator("input").nth(1).fill("")
                        page.locator("input").nth(1).type(self.contrasena, delay=55)
                        pausa_humana(300, 600)
                except Exception:
                    pass
            # 4) Clic "Iniciar sesión".
            try:
                page.locator("button[data-test='login-button']").first.click(timeout=8000)
            except Exception:
                try:
                    page.locator("input").nth(1).press("Enter")
                except Exception:
                    pass
            pausa_humana(2000, 3000)
            print(f"[{self.nombre}] ✅ Credenciales enviadas y login reenviado.")
            # 5) Confirmar. Si Angular todavía no aceptó el token, clicamos el
            #    checkbox del captcha y reenviamos (idéntico a Conciliacion).
            try:
                page.wait_for_url(lambda url: "autenticacion" not in url, timeout=8000)
                return True
            except PlaywrightTimeoutError:
                pass
            try:
                _ctx = getattr(self, "_context", None)
                if _ctx and _extension_capsolver_cargada(_ctx):
                    page.frame_locator(
                        "iframe[src*='recaptcha/api2/anchor']"
                    ).locator("#recaptcha-anchor").click(timeout=5000)
                    print(f"[{self.nombre}] 🧩 Checkbox clicado; espero que la extensión resuelva el desafío...")
                    for _ in range(30):
                        marcado = page.evaluate(
                            "() => !!(document.querySelector('#recaptcha-anchor[aria-checked=\"true\"]') "
                            "|| (document.getElementById('g-recaptcha-response') || {}).value)"
                        )
                        if marcado:
                            break
                        time.sleep(2)
                    page.locator("button[data-test='login-button']").first.click(timeout=8000)
                    pausa_humana(2000, 3000)
            except Exception as e:
                print(f"[{self.nombre}] ⚠️ No pude clicar el checkbox del captcha: {e}")
            return True

        # --- CAPTCHA pre-clic: si el reCAPTCHA YA está visible antes de clicar
        # "Iniciar sesión": con extensión (Chromium+ext) usar extensión+API;
        # SIN extensión (Edge/Firefox) → NO resolver por API aquí, cerrar y relanzar con Chromium+extensión.
        _con_extension = getattr(self, "_capsolver_extension", False)
        captcha_resuelto_antes = False
        try:
            hay_captcha_pre, sitekey_pre = _detectar_recaptcha(page, timeout_ms=1000)
        except Exception:
            hay_captcha_pre, sitekey_pre = False, None
        if hay_captcha_pre:
            if _con_extension:
                print(f"[{self.nombre}] 🧩 reCAPTCHA visible antes del clic (Chromium); "
                      f"ingresando credenciales ANTES de resolver con CapSolver...")
                try:
                    page.locator("input").nth(0).click(); page.locator("input").nth(0).fill("")
                    page.locator("input").nth(0).type(self.usuario, delay=55)
                    pausa_humana(300, 600)
                    page.locator("input").nth(1).click(); page.locator("input").nth(1).fill("")
                    page.locator("input").nth(1).type(self.contrasena, delay=55)
                    pausa_humana(400, 800)
                except Exception as e:
                    print(f"[{self.nombre}] No pude re-escribir credenciales antes del captcha (extensión): {e}")
                print(f"[{self.nombre}] ⏳ Espero hasta 45 s a que la extensión resuelva "
                      f"el captcha...")
                captcha_resuelto_antes = _esperar_captcha_resuelto_en_pagina(page, timeout_seg=45)
                if not captcha_resuelto_antes and sitekey_pre:
                    captcha_resuelto_antes = _resolver_captcha_con_capsolver(page, sitekey_pre, self.nombre)
            else:
                print(f"[{self.nombre}] 🧩 reCAPTCHA visible en Edge sin extensión → "
                      f"cerrando y relanzando con Chromium + CapSolver extension...")
                try:
                    self._motivo_login_fallido = SEÑAL_CAPTCHA_REQUIERE_CHROMIUM
                except Exception:
                    pass
                try:
                    self._captcha_requiere_chromium = True
                except Exception:
                    pass
                return False
        pausa_humana(800, 1500)

        try:
            boton = page.locator("button[data-test='login-button']").first
            boton.wait_for(state="visible", timeout=8000)
            boton.click()
        except Exception:
            try:
                page.locator("button").filter(
                    has_text=re.compile(r"iniciar\s+sesi[oó]n", re.IGNORECASE)
                ).first.click()
            except Exception:
                pass
        # Formulario enviado: soltar el turno de arranque (ver sesiones.py)
        _al_enviar = getattr(self, "_al_enviar_login", None)
        if _al_enviar:
            try:
                _al_enviar()
            except Exception:
                pass
        # Espera POST-clic igual que Conciliacion (2-3s): Bancolombia tarda
        # varios segundos en responder al servidor y SOLTAR el reCAPTCHA. Si se
        # mira antes (300ms), el captcha todavía no existe en el DOM y la
        # detección devuelve False → el flag nunca se pone y se reintenta en
        # Edge en vez de Chromium+CapSolver. (Causa raíz del bug 16/10.)
        pausa_humana(2000, 3000)
        # --- CAPTCHA post-clic: Bancolombia soltó el reCAPTCHA tras bloquear el
        # login. Con extensión se resuelve con extensión (credenciales primero).
        # SIN extensión → relanzar con Chromium+extensión (NO resolver por API).
        try:
            hay_captcha, sitekey_captcha = _detectar_recaptcha(page)
        except Exception:
            hay_captcha, sitekey_captcha = False, None
        if hay_captcha and not captcha_resuelto_antes:
            if not _con_extension:
                print(f"[{self.nombre}] 🧩 Bancolombia soltó el reCAPTCHA en Edge sin extensión tras bloquear el login → "
                      f"cerrando y relanzando con Chromium + CapSolver extension...")
                try:
                    self._motivo_login_fallido = SEÑAL_CAPTCHA_REQUIERE_CHROMIUM
                except Exception:
                    pass
                try:
                    self._captcha_requiere_chromium = True
                except Exception:
                    pass
                return False
            if not _reenviar_con_captcha(
                    "Bancolombia soltó el reCAPTCHA tras bloquear el login",
                    sitekey=sitekey_captcha, timeout_seg=60):
                # No se pudo resolver el captcha (sin sitekey, sin saldo,
                # timeout…): plan B manual — avisar y dar tiempo a resolverlo
                # a mano.
                try:
                    _enviar(self.chat_id,
                            f"🧩 CapSolver no pudo resolver el CAPTCHA de *{self.nombre}*. Resuélvelo en 90 s.",
                            parse_mode="Markdown")
                except Exception:
                    pass
                # Espera visible: antes eran 90 s en silencio y parecía colgado.
                for _restante in range(90, 0, -15):
                    print(f"[{self.nombre}] ⏳ Plan B manual: resuelve el captcha a mano "
                          f"({_restante}s restantes)...")
                    time.sleep(15)
        # Esperar a salir de la pantalla de autenticación (máx. 45 s). Mientras
        # tanto se cierra el "Mensaje informativo" antifraude de Bancolombia
        # ("Antes de hacer clic, confirma ¿Recibiste un mensaje sobre una
        # multa…"), que aparece tras enviar la clave y bloquea el ingreso
        # (10/10: tres cuentas no entraban por esto).
        _limite = time.time() + 45
        _avisos_cerrados = 0
        _captcha_tarde_atendido = False
        # Los campos vacíos no siempre significan "el banco borró las credenciales"
        # (Angular también los vacía mientras procesa el envío). Para no reenviar el
        # login en cadena, se exige que sigan vacíos _CREDS_VACIAS_S segundos y,
        # si NO hay captcha visible, se limita el número de re-envíos.
        _t_creds_vacias     = 0.0
        _CREDS_VACIAS_S     = 6.0
        _REENVIOS_SIN_CAPTCHA = 1
        _reenvios_sin_captcha = 0

        while True:
            try:
                if "autenticacion" not in page.url:
                    break
            except Exception:
                pass
            # ── Captcha / credenciales borradas durante la espera ──────────
            # Bancolombia suelta el reCAPTCHA varios segundos después del clic
            # y, al soltarlo, BORRA usuario y clave. El orden real que funciona
            # a mano es: re-escribir credenciales → resolver el captcha → recién
            # entonces clic "Iniciar sesión".
            try:
                _hay_tarde, _sitekey_tarde = _detectar_recaptcha(page, timeout_ms=300)
            except Exception:
                _hay_tarde, _sitekey_tarde = False, None
            if not getattr(self, "_capsolver_extension", False):
                # Sin extensión (Edge): NO resolver por API. Cerrar e ir al carril Chromium + extensión.
                try:
                    _v0 = page.locator("input").nth(0).input_value(timeout=300) or ""
                    _v1 = page.locator("input").nth(1).input_value(timeout=300) or ""
                except Exception:
                    _v0, _v1 = None, None
                _creds_borradas = (_v0 == "" and _v1 == "")
                _disparar = _creds_borradas or (_hay_tarde and not _captcha_tarde_atendido)
                if _disparar:
                    print(f"[{self.nombre}] 🧩 reCAPTCHA/credenciales borradas en Edge sin extensión "
                          f"(captcha={_hay_tarde}, creds_borradas={_creds_borradas}) → "
                          f"cerrando Edge e intentando con Chromium + CapSolver extension...")
                    try:
                        self._captcha_requiere_chromium = True
                    except Exception:
                        pass
                    try:
                        self._motivo_login_fallido = SEÑAL_CAPTCHA_REQUIERE_CHROMIUM
                    except Exception:
                        pass
                    return False
                # Si no hay captcha visible pero parece haber problema, también forzamos relanzamiento
                if _hay_tarde and not _captcha_tarde_atendido and not _con_extension:
                    print(f"[{self.nombre}] 🧩 reCAPTCHA detectado en Edge sin extensión → "
                          f"cerrando Edge e intentando con Chromium + CapSolver extension...")
                    try:
                        self._captcha_requiere_chromium = True
                    except Exception:
                        pass
                    try:
                        self._motivo_login_fallido = SEÑAL_CAPTCHA_REQUIERE_CHROMIUM
                    except Exception:
                        pass
                    return False
            else:
                # Carril Chromium+extensión: Bancolombia puede ser persistente con el captcha
                # (borra credenciales y lo vuelve a lanzar tras reenviar). Aquí envolvemos SOLO
                # el bloque de reingreso de credenciales + resolución de captcha + click en un
                # ciclo persistente, sin afectar otros tipos de error.
                try:
                    _v0 = page.locator("input").nth(0).input_value(timeout=300) or ""
                    _v1 = page.locator("input").nth(1).input_value(timeout=300) or ""
                except Exception:
                    _v0, _v1 = None, None
                _creds_borradas = (_v0 == "" and _v1 == "")
                # Campos vacíos NO siempre significan "Bancolombia borró las
                # credenciales": Angular también los vacía mientras procesa el
                # envío. Antes eso disparaba un re-envío inmediato, y otro, y
                # otro (log 16:02: "Re-envío #1 ... captcha=False,
                # creds_borradas=True") hasta agotar los 12 intentos: una cadena
                # de inicios de sesión reales que el banco castiga con el
                # bloqueo temporal. Sin captcha visible se exige que sigan
                # vacíos _CREDS_VACIAS_S segundos.
                if _creds_borradas and not _hay_tarde:
                    if _t_creds_vacias == 0.0:
                        _t_creds_vacias = time.time()
                    _creds_borradas = (time.time() - _t_creds_vacias) >= _CREDS_VACIAS_S
                else:
                    _t_creds_vacias = 0.0
                _disparar = _creds_borradas or (_hay_tarde and not _captcha_tarde_atendido)
                if _disparar:
                    _captcha_tarde_atendido = True
                    _intentos_captcha = 0
                    _max_intentos_captcha = 12          # persistencia alta, solo para captcha
                    _limite_captcha = time.time() + 180  # tope de seguridad del ciclo captcha
                    while time.time() < _limite_captcha:
                        # Si el banco ya contestó "Debes esperar", insistir solo
                        # alarga el bloqueo: se sale y lo clasifica el flujo
                        # normal (que además registra la espera).
                        try:
                            if self._detectar_pagina_espera(page):
                                print(f"[{self.nombre}] ⏳ Bancolombia pidió esperar durante "
                                      f"el captcha — dejo de reenviar el login.")
                                _limite = time.time() + 1
                                break
                        except Exception:
                            pass
                        # Salir de /autenticacion (éxito o error del banco) lo
                        # decide el flujo de abajo, para no dar por bueno un
                        # /error-page.
                        try:
                            if "autenticacion" not in page.url:
                                break
                        except Exception:
                            pass
                        try:
                            _hay_tarde, _sitekey_tarde = _detectar_recaptcha(page, timeout_ms=300)
                        except Exception:
                            _hay_tarde, _sitekey_tarde = False, None
                        try:
                            _v0 = page.locator("input").nth(0).input_value(timeout=300) or ""
                            _v1 = page.locator("input").nth(1).input_value(timeout=300) or ""
                        except Exception:
                            _v0, _v1 = None, None
                        _creds_borradas = (_v0 == "" and _v1 == "")
                        if _creds_borradas and not _hay_tarde:
                            if _t_creds_vacias == 0.0:
                                _t_creds_vacias = time.time()
                            _creds_borradas = (
                                time.time() - _t_creds_vacias) >= _CREDS_VACIAS_S
                        else:
                            _t_creds_vacias = 0.0
                        # Si ya no hay señal de captcha/credenciales borradas, salimos del ciclo captcha
                        # y dejamos que el flujo normal continúe (espera/login/otros errores).
                        if not (_hay_tarde or _creds_borradas):
                            _limite = time.time() + 45
                            _avisos_cerrados = 0
                            break
                        # Cerrar aviso antifraude si reaparece (no afecta la lógica de captcha)
                        try:
                            if self._cerrar_aviso_informativo(page):
                                _avisos_cerrados = min(_avisos_cerrados + 1, 3)
                        except Exception:
                            pass
                        _intentos_captcha += 1
                        if _intentos_captcha > _max_intentos_captcha:
                            print(f"[{self.nombre}] ⚠️ Captcha persistente en Chromium ({_max_intentos_captcha} intentos); "
                                  f"dejo vencer el flujo para reintentar con sesión nueva.")
                            _limite = time.time() + 1
                            break
                        # Sin captcha visible solo se reescriben las credenciales
                        # un par de veces: cada re-envío extra es un inicio de
                        # sesión real y el banco lo penaliza.
                        if not _hay_tarde:
                            _reenvios_sin_captcha += 1
                            if _reenvios_sin_captcha > _REENVIOS_SIN_CAPTCHA:
                                print(f"[{self.nombre}] ⚠️ Credenciales vacías sin captcha "
                                      f"tras {_REENVIOS_SIN_CAPTCHA} re-envíos — dejo de "
                                      f"insistir para no bloquear el inicio de sesión.")
                                _limite = time.time() + 1
                                break
                        print(f"[{self.nombre}] 🧩 Re-envío captcha #{_intentos_captcha} en Chromium "
                              f"(captcha={_hay_tarde}, creds_borradas={_creds_borradas}) → "
                              f"re-escribo credenciales, resuelvo y clic 'Iniciar sesión'.")
                        _t_reenvio = time.time()
                        _ok_reenvio = _reenviar_con_captcha(
                            "Bancolombia borró usuario/clave o soltó el captcha",
                            sitekey=_sitekey_tarde, timeout_seg=60)
                        print(f"[{self.nombre}] 🧩 Re-envío #{_intentos_captcha} terminado "
                              f"en {time.time() - _t_reenvio:.0f}s (ok={_ok_reenvio}).")
                        _limite = time.time() + 45
                        _avisos_cerrados = 0   # el aviso antifraude puede volver a salir
                        if _ok_reenvio:
                            try:
                                if "autenticacion" not in page.url:
                                    return True
                            except Exception:
                                pass
                        # Pequeña espera antes de reevaluar (Bancolombia puede volver a soltar captcha)
                        try:
                            page.wait_for_timeout(1500)
                        except Exception:
                            time.sleep(1.5)
                    else:
                        # Se agotó el tiempo del ciclo captcha sin salir de autenticación por éxito claro
                        print(f"[{self.nombre}] ⚠️ Ciclo de captcha persistente agotó el tiempo en Chromium; "
                              f"dejo vencer el flujo para reintentar con sesión nueva.")
                        _limite = time.time() + 1
            if time.time() > _limite:
                # Qué muestra la página y qué botones hay (para saber por qué
                # no entra). innerText no incluye lo escrito en los campos,
                # así que no expone credenciales.
                try:
                    _info = page.evaluate("""() => ({
                        txt: document.body?.innerText || '',
                        btns: [...document.querySelectorAll('button, a[role="button"]')]
                              .filter(b => { const r = b.getBoundingClientRect();
                                             return r.width > 0 && r.height > 0; })
                              .map(b => (b.textContent || b.getAttribute('aria-label') || '').trim())
                              .filter(t => t).slice(0, 8),
                    })""")
                    _txt = " ".join(_info["txt"].split())[:300]
                    print(f"[{self.nombre}] 🔎 Login sin avanzar — la página dice: '{_txt}'"
                          f"  | botones: {_info['btns']}")
                except Exception:
                    pass
                # El caller reintenta; startup_done se señala al final de run()
                return False
            # Reintentar cerrar el aviso, pero MÁXIMO 3 veces: si tras 3 clics la
            # página no avanza (pasa cuando la CPU está saturada y la página va
            # congelada), se deja vencer el tiempo y el caller reintenta con una
            # sesión nueva, en vez de machacar "Iniciar sesión" decenas de veces
            # —lo que podría disparar la detección de fraude de Bancolombia—.
            if _avisos_cerrados < 3 and self._cerrar_aviso_informativo(page):
                _avisos_cerrados += 1
            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1)
        if self._detectar_pagina_espera(page):
            with _bloqueo_lock:
                # La espera CRECE si el banco sigue pidiendo esperar: 15, 30, 60, 120 y máx. 240 min.
                # Insistir cada 15 min cuando el aviso dura horas solo agrava las cosas.
                _bloqueo["n"] += 1
                espera_min = min(_ESPERA_BLOQUEO_MIN * 2 ** (_bloqueo["n"] - 1), 240)
                _bloqueo["hasta"] = time.time() + espera_min * 60
                avisar = time.time() - _bloqueo["aviso"] > 30 * 60
                if avisar:
                    _bloqueo["aviso"] = time.time()
            print(f"[{self.nombre}] 🚫 Bancolombia dice: «Debes esperar para volver a intentarlo — por seguridad "
                  f"no puedes iniciar sesión». No es un login válido. Se pausan los intentos de este PC "
                  f"{espera_min} min (veces seguidas: {_bloqueo['n']}).")
            if not getattr(self, "_motivo_login_fallido", None):
                self._motivo_login_fallido = ("Bancolombia pidió esperar para volver a intentar el inicio de sesión "
                                              "(bloqueo temporal). No se reintenta ahora.")
            if avisar:
                try:
                    _enviar(OWNER_CHAT_ID,
                            "🚫 Bancolombia bloqueó temporalmente el inicio de sesión desde este PC "
                            "(\"Debes esperar para volver a intentarlo\"). Se pausan los intentos "
                            f"{espera_min} min y las cuentas que no estaban abiertas no se activan.")
                except Exception:
                    pass
            return False
        _bloqueo["n"] = 0   # entró de verdad: se reinicia la escalera de esperas
        print(f"[{self.nombre}] Login OK.")
        pausa_humana(200, 400)
        self._aceptar_documentos_si_aparece(page)
        return True

    def _cerrar_aviso_informativo(self, page) -> bool:
        """
        Cierra el "Mensaje informativo" antifraude que Bancolombia muestra al
        entrar. Solo pulsa botones de cierre/continuar (nunca "Sí"/"No", por si
        el aviso hace una pregunta). True si lo cerró.
        """
        # ¿Está el aviso en pantalla y cuál es el texto exacto del botón a pulsar?
        try:
            res = page.evaluate(r"""() => {
                const t = (document.body?.innerText || '').toLowerCase();
                if (!t.includes('mensaje informativo') && !t.includes('antes de hacer clic')) return null;
                const visibles = [...document.querySelectorAll('button, a[role="button"]')]
                    .filter(b => { const r = b.getBoundingClientRect();
                                   return r.width > 0 && r.height > 0; })
                    .map(b => (b.textContent || b.getAttribute('aria-label') || '').trim())
                    .filter(t => t);
                const ok = /^(entendido|continuar|aceptar|cerrar|listo|ok|seguir|ir a|iniciar sesi|ingresar)/i;
                let elegido = null;
                for (const b of visibles) { if (ok.test(b)) { elegido = b; break; } }
                return { botones: visibles.slice(0, 12), elegido };
            }""")
        except Exception:
            return False
        if not res:
            return False
        boton = res["elegido"]
        # Diagnóstico: si no hay botón de cierre, o el "elegido" es sospechoso
        # ("Iniciar sesión" es el del FORMULARIO, no el del modal), se imprimen
        # los botones reales del modal para saber cuál pulsar.
        if not boton or boton.lower().startswith("iniciar"):
            print(f"[{self.nombre}] 🩺 'Mensaje informativo' con botones: {res['botones']}")
        if not boton:
            return False
        # "Iniciar sesión" es el único botón del modal antifraude, pero TAMBIÉN
        # es el del formulario de login. Si solo hay UN botón visible con ese
        # nombre, es el del formulario: clicarlo reenvía el login. Y si el modal
        # sigue en pantalla después de un clic, no se vuelve a pulsar en < 20 s
        # (el log del 16:02 muestra 3 clics en 8 s, cada uno un intento real de
        # inicio de sesión que el banco terminó bloqueando).
        if boton.lower().startswith("iniciar"):
            try:
                _cuantos = page.get_by_role("button", name=boton, exact=False).count()
            except Exception:
                _cuantos = 0
            if _cuantos < 2:
                print(f"[{self.nombre}] 🩺 Un solo botón '{boton}' visible (es el del "
                      f"formulario) — no lo clico para no reenviar el login.")
                return False
            _ahora = time.time()
            if _ahora - getattr(self, "_t_clic_aviso_iniciar", 0.0) < 20:
                return False
            self._t_clic_aviso_iniciar = _ahora
        # Clic REAL de Playwright (dispara el router de Angular mejor que un
        # btn.click() de JS, que a veces no avanza — caso una cuenta 10/10). Si no se
        # puede, cae al clic por JS.
        # OJO: se usa .last y NO .first. El aviso antifraude es un MODAL cuyo
        # botón ("Iniciar sesión") se agreg al FINAL del body; el botón .first
        # con ese mismo nombre es el del FORMULARIO de login, que queda DETRÁS
        # del modal. Clicar .first re-enviaba el login y el aviso reaparecía en
        # bucle (16/10). .last alcanza el botón visible del modal.
        try:
            page.get_by_role("button", name=boton, exact=False).last.click(timeout=2500)
        except Exception:
            try:
                page.evaluate("""(txt) => {
                    let _last = null;
                    for (const b of document.querySelectorAll('button, a[role="button"]')) {
                        const t = (b.textContent || b.getAttribute('aria-label') || '').trim();
                        if (t === txt) _last = b;
                    }
                    if (_last) _last.click();
                }""", boton)
            except Exception:
                return False
        print(f"[{self.nombre}] 🛡️  'Mensaje informativo' de Bancolombia cerrado (botón: '{boton[:30]}')")
        return True

    def _navegar_rapido_a_movimientos(self, page) -> bool:
        """
        Navegación de startup: sidebar Productos → Tus cuentas → Movimientos.
        Solo se llama una vez al iniciar la sesión. El ciclo regular usa _click_buscar().
        """
        try:
            try:
                url_actual = page.url
            except Exception:
                url_actual = ""

            if "/detalles-y-movimientos/" in url_actual:
                print(f"[{self.nombre}] 🚀 Ya en cuentas — click Movimientos")
                saldo_detalles = self._leer_saldo_detalles(page)
                if saldo_detalles:
                    self._saldo_en_detalles = saldo_detalles
                self._click_tab_por_texto(page, "Movimientos")
                return True

            print(f"[{self.nombre}] 🚀 Navigando via sidebar...")
            sidebar_sel = (
                "a:has-text('Productos'), mat-list-item:has-text('Productos'), "
                "span:has-text('Productos'), a[href*='/home']"
            )
            try:
                page.wait_for_selector(sidebar_sel, timeout=8000)
            except Exception:
                pass

            cuenta_ok = False
            for sel_prod in [
                "a:has-text('Productos')",
                "mat-list-item:has-text('Productos')",
                "span:has-text('Productos')",
                "li:has-text('Productos') a",
            ]:
                try:
                    el = page.locator(sel_prod).first
                    if el.is_visible(timeout=800):
                        el.click()
                        for sel_cta in [
                            "a:has-text('Tus cuentas')",
                            "span:has-text('Tus cuentas')",
                            ":has-text('Tus cuentas')",
                        ]:
                            try:
                                c = page.locator(sel_cta).first
                                if c.is_visible(timeout=800):
                                    c.click()
                                    cuenta_ok = True
                                    break
                            except Exception as _e:
                                if _es_fatal(_e): raise
                                continue
                        if cuenta_ok:
                            break
                except Exception as _e:
                    if _es_fatal(_e): raise
                    continue

            if not cuenta_ok:
                for sel in [
                    "text='Cuenta de Ahorros'", "bc-product-card",
                    "[class*='product-card']",
                    "h2:has-text('Cuenta de Ahorros')",
                    "h2:has-text('Cuenta Corriente')",
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=3000):
                            el.click()
                            cuenta_ok = True
                            pausa_humana(800, 1200)
                            break
                    except Exception as _e:
                        if _es_fatal(_e): raise
                        continue

            saldo_detalles = self._leer_saldo_detalles(page)
            if saldo_detalles:
                self._saldo_en_detalles = saldo_detalles

            self._click_tab_por_texto(page, "Movimientos")
            if not cuenta_ok:
                # "Startup navigation OK" se imprime igual, pero aquí NO se llegó a "Tus cuentas": la página
                # tras el login no tiene el menú de siempre. Se deja constancia (ver movimiento.py).
                self._incidente_sin_menu(page, "arranque")
            print(f"[{self.nombre}] ✅ Startup navigation OK")
            return True

        except Exception as e:
            if _es_fatal(e): raise
            print(f"[{self.nombre}] ⚠️ _navegar_rapido_a_movimientos: {e}")
            return False

    def _aceptar_documentos_si_aparece(self, page) -> bool:
        try:
            if "firmar-documentos" not in page.url:
                return False
            page.evaluate("""() => {
                document.querySelectorAll('input[type="checkbox"]')
                    .forEach(cb => { if (!cb.checked) cb.click(); });
            }""")
            pausa_humana(400, 700)
            for sel in [
                "button:has-text('Aceptar')", "button:has-text('Acepto')",
                "[class*='btn']:has-text('Aceptar')", "input[type='submit']",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click()
                        pausa_humana(800, 1500)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _parchar_prefs(self):
        """
        Parchea el archivo Preferences de Chrome para que no muestre el popup
        de recuperación de sesión (crash recovery) al arrancar.
        """
        # Con CAPSOLVER_DESDE_INICIO se usa el perfil "_chromium": parchear las
        # dos carpetas para que el popup de crash-recovery no salga en ninguna.
        try:
            _dirs = self._dirs_perfil()
        except Exception:
            _dirs = [self.profile_dir]
        for _dir_perfil in _dirs:
            prefs_path = os.path.join(_dir_perfil, "Default", "Preferences")
            if not os.path.exists(prefs_path):
                continue
            try:
                with open(prefs_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
                prefs.setdefault("profile", {})["exit_type"]      = "Normal"
                prefs.setdefault("profile", {})["exited_cleanly"] = True
                with open(prefs_path, "w", encoding="utf-8") as f:
                    json.dump(prefs, f)
            except Exception:
                pass
