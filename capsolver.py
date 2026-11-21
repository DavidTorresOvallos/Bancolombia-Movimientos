# -*- coding: utf-8 -*-
# =====================================================================
# CAPSOLVER.PY
# Responsabilidad: ÚNICA Y EXCLUSIVAMENTE resolver el reCAPTCHA de
# Bancolombia. Es el único archivo del sistema que sabe de CapSolver — el
# resto (login.py, saldo.py, sesiones.py) solo importa de acá lo que
# necesita (la ruta de la extensión, el hook, y las funciones de
# detección/resolución) y nunca implementa nada de captcha por su cuenta.
#
# *** ESTE ARCHIVO LO INTEGRÓ EL EQUIPO — NO TOCAR / NO DEBUGUEAR ***
# Movido tal cual desde login.py, sin modificar ni una línea de la lógica.
# =====================================================================
import os
import time

import requests

# --- CapSolver: resolución automática de reCAPTCHA v2 ---
# La extensión de Chrome (carpeta capsolver_extension junto al script, con
# assets/config.js en reCaptchaMode "click") CLICA el checkbox ella sola dentro
# del navegador. La API (createTask/getTaskResult) queda como respaldo por si
# la extensión no alcanza a marcarlo.
try:
    import config as _cfg_cap
    CAPSOLVER_API_KEY = str(getattr(_cfg_cap, "CAPSOLVER_API_KEY", "") or "").strip()
except Exception:
    CAPSOLVER_API_KEY = ""   # la key vive en config.py (local, fuera del repo)
CAPSOLVER_API_URL = "https://api.capsolver.com"
CAPSOLVER_TIMEOUT_SEG = 180
CAPSOLVER_EXT_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "capsolver_extension"))


def _generar_config_extension() -> None:
    """Escribe capsolver_extension/assets/config.js (ignorado por git) desde la plantilla
    y la CAPSOLVER_API_KEY de config.py. La extension no puede leer config.py."""
    try:
        ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capsolver_extension", "assets")
        plantilla = os.path.join(ext, "config.plantilla.js")
        if not os.path.isfile(plantilla):
            return
        if not CAPSOLVER_API_KEY:
            print("[CapSolver] \u26a0\ufe0f CAPSOLVER_API_KEY vacia en config.py \u2014 la extension no resolvera captchas.")
            return
        with open(plantilla, encoding="utf-8") as f:
            txt = f.read()
        txt = txt.replace("apiKey: ''", "apiKey: " + repr(CAPSOLVER_API_KEY), 1)
        with open(os.path.join(ext, "config.js"), "w", encoding="utf-8", newline="") as f:
            f.write(txt)
    except Exception as e:
        print(f"[CapSolver] no se pudo generar config.js de la extension: {e}")


_generar_config_extension()


def _detectar_recaptcha(page, timeout_ms: int = 1500) -> tuple:
    """Devuelve (hay_captcha_visible, sitekey). El sitekey se busca en el
    atributo data-sitekey del widget o en el parámetro k= del iframe de
    Google — es lo que CapSolver necesita para crear la tarea."""
    hay = False
    for sel in ["iframe[src*='recaptcha']", ".g-recaptcha", "[class*='recaptcha']", "[id*='captcha']"]:
        try:
            if page.locator(sel).first.is_visible(timeout=timeout_ms):
                hay = True
                break
        except Exception:
            continue
    if not hay:
        return False, None
    try:
        sitekey = page.evaluate(
            """() => {
                const conAttr = document.querySelector('[data-sitekey]');
                if (conAttr) return conAttr.getAttribute('data-sitekey');
                const iframe = document.querySelector("iframe[src*='recaptcha']");
                if (iframe) {
                    const m = (iframe.src || '').match(/[?&]k=([^&]+)/);
                    if (m) return decodeURIComponent(m[1]);
                }
                return null;
            }"""
        )
    except Exception as e:
        print(f"⚠️ No pude extraer el sitekey del reCAPTCHA: {e}")
        sitekey = None
    return True, sitekey


def _esperar_captcha_resuelto_en_pagina(page, timeout_seg: int = 60) -> bool:
    """Espera a que el reCAPTCHA aparezca RESUELTO DENTRO del navegador — así
    lo deja la extensión de CapSolver tras clicar el checkbox ella sola
    (aria-checked=true en #recaptcha-anchor, span .recaptcha-checkbox-checked,
    o el textarea g-recaptcha-response con valor). Retorna True si quedó
    resuelto antes del timeout."""
    inicio = time.time()
    while time.time() - inicio < timeout_seg:
        try:
            listo = page.evaluate(
                """() => {
                    const anchor = document.querySelector('#recaptcha-anchor');
                    if (anchor && anchor.getAttribute('aria-checked') === 'true') return true;
                    if (document.querySelector('.recaptcha-checkbox-checked')) return true;
                    const ta = document.getElementById('g-recaptcha-response');
                    if (ta && (ta.value || '').length > 20) return true;
                    return false;
                }"""
            )
            if listo:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _capsolver_resolver_recaptcha(sitekey: str, url_pagina: str, nombre_cuenta: str) -> str | None:
    """Crea la tarea de reCAPTCHA v2 en CapSolver y espera el token
    (g-recaptcha-response). Retorna el token, o None si falló/tardó demás."""
    task_id = None
    for tipo_tarea in ("ReCaptchaV2TaskProxyLess", "ReCaptchaV2EnterpriseTaskProxyLess"):
        try:
            data = requests.post(
                f"{CAPSOLVER_API_URL}/createTask",
                json={
                    "clientKey": CAPSOLVER_API_KEY,
                    "task": {
                        "type": tipo_tarea,
                        "websiteURL": url_pagina,
                        "websiteKey": sitekey,
                    },
                },
                timeout=30,
            ).json()
        except Exception as e:
            print(f"⚠️ [CapSolver] createTask falló: {e}")
            return None
        if data.get("errorId"):
            print(f"⚠️ [CapSolver] createTask ({tipo_tarea}) error: "
                  f"{data.get('errorCode')} — {data.get('errorDescription')}")
            continue
        task_id = data.get("taskId")
        print(f"[{nombre_cuenta}] [CapSolver] Tarea {task_id} creada ({tipo_tarea}).")
        break
    if not task_id:
        return None
    inicio = time.time()
    while time.time() - inicio < CAPSOLVER_TIMEOUT_SEG:
        time.sleep(5)
        try:
            data = requests.post(
                f"{CAPSOLVER_API_URL}/getTaskResult",
                json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id},
                timeout=30,
            ).json()
        except Exception as e:
            print(f"⚠️ [CapSolver] getTaskResult falló: {e}")
            continue
        if data.get("errorId"):
            print(f"⚠️ [CapSolver] getTaskResult error: "
                  f"{data.get('errorCode')} — {data.get('errorDescription')}")
            return None
        if data.get("status") == "ready":
            token = (data.get("solution") or {}).get("gRecaptchaResponse")
            print(f"[{nombre_cuenta}] [CapSolver] CAPTCHA resuelto en {int(time.time() - inicio)}s.")
            return token
    print(f"⚠️ [CapSolver] Timeout ({CAPSOLVER_TIMEOUT_SEG}s) esperando la solución.")
    return None


# Hook que se instala ANTES de que cargue la página: hace que
# grecaptcha.getResponse()/execute() devuelvan nuestro token en el momento
# en que Angular los llame al enviar el formulario.
HOOK_GRECAPTCHA_JS = """
(() => {
  if (window.__capsolver_hook) return;
  window.__capsolver_hook = true;
  window.__capsolver_token = '';
  const parchear = (g) => {
    if (!g || g.__capsolver_patched) return;
    g.__capsolver_patched = true;
    try { g.getResponse = () => window.__capsolver_token; } catch (e) {}
    try { g.execute = () => Promise.resolve(window.__capsolver_token); } catch (e) {}
  };
  let _g = window.grecaptcha;
  if (_g) parchear(_g);
  try {
    Object.defineProperty(window, 'grecaptcha', {
      configurable: true,
      get: () => _g,
      set: (v) => { _g = v; parchear(v); },
    });
  } catch (e) {}
})();
"""


def _extension_capsolver_cargada(context) -> bool:
    """True si la extensión de CapSolver quedó montada (aparece su service
    worker o background page). Espera hasta 8s: el worker MV3 no arranca
    siempre de inmediato."""
    print(f"[CapSolver] Ruta de la extensión: {CAPSOLVER_EXT_PATH}")
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            if context.service_workers or context.background_pages:
                return True
        except Exception:
            pass
        try:
            context.wait_for_event("serviceworker", timeout=1500)
            return True
        except Exception:
            continue
    return False

def _diagnosticar_captcha(page, nombre_cuenta: str) -> None:
    """Vertebrado de diagnóstico (una sola vez por intento): imprime qué
    reCAPTCHA hay REALMENTE en el DOM. Sirve para saber si Bancolombia monta
    un checkbox v2, un invisible, o el badge, y por qué el click al checkbox
    falla."""
    try:
        info = page.evaluate(r"""() => {
            const src = (f) => (f.src || '').slice(0, 180);
            const all = [...document.querySelectorAll('iframe')];
            const ta = document.getElementById('g-recaptcha-response');
            const anchor = document.querySelector('#recaptcha-anchor');
            const modal = [...document.querySelectorAll('button, a[role="button"]')]
                .filter(b => { const r = b.getBoundingClientRect();
                               return r.width > 0 && r.height > 0; })
                .map(b => (b.textContent || b.getAttribute('aria-label') || '').trim())
                .filter(t => t).slice(0, 10);
            const recaptchaIframes = all.filter(f => src(f).includes('recaptcha')).map(src);
            const anchorIframes = all.filter(f => src(f).includes('anchor')).map(src);
            const sitekeys = [...document.querySelectorAll('[data-sitekey]')]
                .map(w => w.getAttribute('data-sitekey'));
            return {
                recaptchaIframes, anchorIframes, sitekeys,
                tokenLen: ta ? (ta.value || '').length : -1,
                anchorPresent: !!anchor,
                anchorChecked: anchor ? anchor.getAttribute('aria-checked') : null,
                hayMensajeInformativo: (document.body?.innerText || '').toLowerCase()
                                        .includes('mensaje informativo'),
                botonesVisibles: modal,
            };
        }""")
        print(f"[{nombre_cuenta}] 🩺 captcha: {info}")
    except Exception as e:
        print(f"[{nombre_cuenta}] 🩺 No pude diagnosticar el captcha: {e}")


def _inyectar_token_recaptcha(page, token: str) -> bool:
    """MEJORADA: 1) activa el hook instalado al arrancar, 2) llena el textarea
    oculto, 3) busca RECURSIVAMENTE en ___grecaptcha_cfg cualquier callback del
    widget (así ng-recaptcha/Angular actualiza su FormControl) y lo dispara con
    el token, 4) dispara data-callback clásico si existe."""
    try:
        page.evaluate("() => {}", )  # no-op para asegurar conexión
        page.evaluate("(t) => { window.__capsolver_token = t; }", token)
        page.evaluate(
            """(token) => {
                const ta = document.getElementById('g-recaptcha-response');
                if (ta) { ta.value = token; ta.innerHTML = token; }
                const vistos = new Set();
                const invocar = (obj, prof) => {
                    if (!obj || prof > 8 || vistos.has(obj)) return;
                    vistos.add(obj);
                    for (const k of Object.keys(obj)) {
                        let v;
                        try { v = obj[k]; } catch (e) { continue; }
                        if (typeof v === 'function' && (k === 'callback' || k === 'resolvedCallback')) {
                            try { v(token); } catch (e) {}
                        } else if (typeof v === 'object' && v !== null) {
                            invocar(v, prof + 1);
                        }
                    }
                };
                try { if (window.___grecaptcha_cfg) invocar(window.___grecaptcha_cfg, 0); } catch (e) {}
                try {
                    const widget = document.querySelector('.g-recaptcha[data-callback]');
                    if (widget) {
                        const cb = window[widget.getAttribute('data-callback')];
                        if (typeof cb === 'function') cb(token);
                    }
                } catch (e) {}
            }""",
            token,
        )
        return True
    except Exception as e:
        print(f"⚠️ No pude inyectar el token de reCAPTCHA: {e}")
        return False

def _resolver_captcha_con_capsolver(page, sitekey: str, nombre_cuenta: str) -> bool:
    """Resuelve el captcha por API (createTask -> getTaskResult -> inyección
    del token). Retorna True solo si el token quedó inyectado en la página."""
    print(f"[{nombre_cuenta}] 🧩 reCAPTCHA visible en el login, intentando con CapSolver (API)...")
    if not sitekey:
        print(f"[{nombre_cuenta}] ⚠️ CAPTCHA visible pero no pude extraer el sitekey.")
        return False
    inicio = time.time()
    token = _capsolver_resolver_recaptcha(sitekey, page.url, nombre_cuenta)
    if not token:
        return False
    ok = _inyectar_token_recaptcha(page, token)
    if ok:
        print(f"[{nombre_cuenta}] ✅ CAPTCHA resuelto por CapSolver en {int(time.time() - inicio)}s.")
    return ok
