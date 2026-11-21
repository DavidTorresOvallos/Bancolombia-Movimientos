# -*- coding: utf-8 -*-
# =====================================================================
# MOVIMIENTO.PY
# Responsabilidad: detección de transacciones y ciclo de monitoreo.
# - Intercepción XHR de la API de Bancolombia (cola de respuestas)
# - Fingerprinting y comparación de transacciones (nuevas vs. conocidas)
# - Toggle Detalles ↔ Movimientos (ciclo principal _click_buscar)
# - Fallback DOM scraping cuando el XHR no responde
# - Paginación de movimientos
# - Reintentos cuando Bancolombia atrasa la propagación de la tx
# - Caché de transacciones conocidas (por cuenta, en disco)
# =====================================================================
import os
import re
import time
import json
import random
import threading
import queue as _queue
from datetime import datetime

from notificar import _parsear_pesos, _es_fatal, OWNER_CHAT_ID, bot
from filtro_dom import filtrar_filas_por_saldo

# ── Constantes de monitoreo ────────────────────────────────────────
API_TXS_KW       = "ch-ms-deposits/account/transactions"
MESES_ES         = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"]

CACHE_DIR        = os.path.join(os.path.dirname(__file__), "movimientos_cache")

# ── Funciones libres de utilidad ───────────────────────────────────
def extraer_saldo(page) -> str | None:
    """
    Extrae el saldo real de la pagina de Bancolombia.
    Prioridad 1: etiqueta semantica 'Saldo disponible' / 'Saldo'.
    Prioridad 2: primer elemento hoja visible con $ (DOM order, no el mayor).
    Tomar el mayor valor causaba que montos de transacciones pisaran el saldo.
    """
    try:
        resultado = page.evaluate(r"""() => {
            const patronMonto = /\$\s*[\d\.,]+/;
            const etiquetas = /saldo\s*(disponible|actual|total)?|available\s*balance/i;

            // Paso 1: buscar por etiqueta semantica
            for (const el of document.querySelectorAll('*')) {
                const txt = (el.textContent || '').trim();
                if (!etiquetas.test(txt) || txt.length > 80) continue;
                const contenedor = el.parentElement || el;
                for (const hijo of contenedor.querySelectorAll('*')) {
                    if (hijo.children.length > 0) continue;
                    const rect = hijo.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const val = (hijo.textContent || '').trim();
                    if (patronMonto.test(val) && val.length < 30) return val;
                }
            }

            // Paso 2: primer elemento hoja visible con $ en DOM order
            for (const tag of ['span','b','strong','p','h1','h2','h3','td']) {
                for (const el of document.querySelectorAll(tag)) {
                    if (el.children.length > 0) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const val = (el.textContent || '').trim();
                    if (val.length > 0 && val.length < 30 && patronMonto.test(val))
                        return val;
                }
            }
            return null;
        }""")
        return resultado or None
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════
# MovimientoMixin
# Toda la lógica de detección y ciclo de monitoreo.
# Se mezcla en MonitorThread vía herencia múltiple (sesiones.py).
# ══════════════════════════════════════════════════════════════════
class MovimientoMixin:

    # ── Fingerprint ────────────────────────────────────────────────
    @staticmethod
    def _fingerprint(tx: dict) -> str:
        """
        Clave base única para una transacción.
        Usa todos los campos disponibles: hora exacta, ID único, y como
        fallback fecha|desc|monto|ref1|ref2|tipo|office.
        """
        fecha  = tx.get("transactionDate", "")
        desc   = (tx.get("description") or "").strip()
        amount = float(tx.get("amount", 0))
        ref1   = (tx.get("reference1") or "").strip()
        ref2   = (tx.get("reference2") or "").strip()

        _type_raw   = tx.get("type")
        _office_raw = tx.get("office")
        if isinstance(_type_raw, dict):
            tipo = str(_type_raw.get("code") or _type_raw.get("name") or "").strip()
        else:
            tipo = str(_type_raw or "").strip()
        if isinstance(_office_raw, dict):
            office = str(_office_raw.get("code") or _office_raw.get("name") or "").strip()
        else:
            office = str(_office_raw or "").strip()

        hora = ""
        for campo in ("transactionTime", "time", "operationTime", "hour",
                      "valueTime", "creationTime", "domHora"):
            v = str(tx.get(campo) or "").strip()
            if v and v not in ("None", "null", "0", ""):
                hora = v; break

        tx_id = ""
        for campo in ("transactionId", "operationId", "sequenceNumber",
                      "id", "trackingId", "consecutivo"):
            v = str(tx.get(campo) or "").strip()
            if v and v not in ("None", "null", "0", ""):
                tx_id = v; break

        if tx_id:
            return f"{fecha}|{desc}|{amount:.2f}|ID:{tx_id}"
        if hora:
            return f"{fecha}T{hora}|{desc}|{amount:.2f}|{ref1}|{tipo}|{office}"
        return f"{fecha}|{desc}|{amount:.2f}|{ref1}|{ref2}|{tipo}|{office}"

    @staticmethod
    def _es_fecha_reciente(date_str: str, dias: int = 3) -> bool:
        """True si la fecha API ('2026/08/04') es de hoy o en los últimos `dias` días."""
        try:
            dt = datetime.strptime(str(date_str).strip(), "%Y/%m/%d")
            return (datetime.now() - dt).days <= dias
        except Exception:
            return True

    # ── Detección de transacciones nuevas ─────────────────────────
    def _detectar_nuevas(self, txs: list) -> list:
        """
        Detecta txs realmente nuevas comparando conteos máximos.
        txs_conocidas[base_fp] = max de veces que esa tx apareció simultáneamente.
        Si el ciclo actual muestra MÁS instancias → son nuevas.
        """
        base_fps = [MovimientoMixin._fingerprint(tx) for tx in txs]
        nuevas = []
        seen_este_ciclo: dict = {}
        for tx, base_fp in zip(txs, base_fps):
            n_ya = seen_este_ciclo.get(base_fp, 0)
            max_conocido = self.txs_conocidas.get(base_fp, 0)
            if n_ya >= max_conocido:
                nuevas.append(tx)
            seen_este_ciclo[base_fp] = n_ya + 1
        return nuevas

    def _actualizar_conocidas(self, txs: list):
        """Actualiza txs_conocidas con el conteo máximo simultáneo. Nunca reduce."""
        conteo: dict = {}
        for tx in txs:
            fp = MovimientoMixin._fingerprint(tx)
            conteo[fp] = conteo.get(fp, 0) + 1
        for fp, count in conteo.items():
            if count > self.txs_conocidas.get(fp, 0):
                self.txs_conocidas[fp] = count

    @staticmethod
    def _n_conocidas_total(txs_conocidas: dict) -> int:
        return sum(txs_conocidas.values())

    @staticmethod
    def _diff_snapshot(txs_nuevas: list, snapshot_viejo: list) -> list:
        """
        Diff contra snapshot pre-caída: más preciso que _detectar_nuevas porque
        usa el estado REAL antes del crash, no el máximo acumulado de toda la sesión.
        """
        old_counter: dict = {}
        for tx in snapshot_viejo:
            fp = MovimientoMixin._fingerprint(tx)
            old_counter[fp] = old_counter.get(fp, 0) + 1

        nuevas: list = []
        seen: dict = {}
        for tx in txs_nuevas:
            fp = MovimientoMixin._fingerprint(tx)
            n_visto = seen.get(fp, 0)
            if n_visto >= old_counter.get(fp, 0):
                nuevas.append(tx)
            seen[fp] = n_visto + 1
        return nuevas

    # ── Cola XHR ──────────────────────────────────────────────────
    def _route_ampliar_txs(self, route, request):
        """Intercepta el request de transacciones e intenta ampliar el page size a 50."""
        import urllib.parse as _up
        try:
            url = request.url
            if not self._api_url_logueada:
                self._api_url_logueada = True
                print(f"[{self.nombre}] 🔗 API request: {request.method} {url}")

            parsed = _up.urlparse(url)
            params = _up.parse_qs(parsed.query, keep_blank_values=True)

            modified = False
            for _param in ("size", "limit", "quantity", "rows", "count",
                           "pageSize", "page_size", "numRegistros"):
                if _param in params:
                    old_val = params[_param][0]
                    try:
                        if int(old_val) < 50:
                            params[_param] = ["50"]
                            modified = True
                            print(f"[{self.nombre}] 📊 API ampliada: "
                                  f"{_param}={old_val} → 50 txs")
                    except ValueError:
                        pass
                    break

            if modified:
                new_query = _up.urlencode({k: v[0] for k, v in params.items()})
                new_url   = _up.urlunparse(parsed._replace(query=new_query))
                route.continue_(url=new_url)
            else:
                route.continue_()
        except Exception:
            route.continue_()

    def _on_api_response(self, response):
        """Intercepta respuestas de la API de transacciones y las encola."""
        try:
            if response.status not in (200, 201):
                return
            if API_TXS_KW not in response.url:
                return
            data = response.json()
            txs  = data.get("data", {}).get("transactions", [])
            if isinstance(txs, list) and txs:
                self._tx_api_queue.put(txs)
                print(f"[{self.nombre}] 🌐 API: {len(txs)} transacciones recibidas")
                if not self._api_campos_logueados:
                    self._api_campos_logueados = True
                    campos = {k: v for k, v in txs[0].items() if v is not None}
                    print(f"[{self.nombre}] 🔎 Campos API (1ª tx): {list(campos.keys())}")
                    for k, v in txs[0].items():
                        if any(x in k.lower() for x in ("time","hour","id","seq","track","consec")):
                            print(f"[{self.nombre}]    · {k}: {v}")
        except Exception as e:
            print(f"[{self.nombre}] ⚠️ _on_api_response: {e}")

    def _drenar_queue(self) -> list | None:
        """Vacía la cola de API y retorna la última respuesta (o None)."""
        resultado = None
        try:
            while True:
                resultado = self._tx_api_queue.get_nowait()
        except _queue.Empty:
            pass
        return resultado

    def _esperar_api(self, timeout: float = 8.0, page=None) -> list | None:
        """
        Espera respuesta de la API con heartbeats cada 150ms.
        Los heartbeats mantienen el event loop de Playwright activo para que
        response.json() complete su IPC sin bloquearse.
        """
        TICK = 0.15
        deadline = time.time() + timeout
        while True:
            if self._renderer_muerto.is_set():
                return None
            restante = deadline - time.time()
            if restante <= 0:
                return None
            espera = min(TICK, restante)
            try:
                txs = self._tx_api_queue.get(timeout=espera)
                while not self._tx_api_queue.empty():
                    txs = self._tx_api_queue.get_nowait()
                return txs
            except _queue.Empty:
                if page:
                    try:
                        page.evaluate("() => 1")
                    except Exception:
                        pass

    # ── Toggle Detalles ↔ Movimientos ─────────────────────────────
    @staticmethod
    def _click_tab_por_texto(page, texto: str) -> bool:
        """Click en tab Angular Material por texto. Prueba múltiples selectores."""
        SELECTORES = [
            f".mat-tab-label:has-text('{texto}')",
            f".mat-mdc-tab:has-text('{texto}')",
            f"[role='tab']:has-text('{texto}')",
            f"mat-tab-header [tabindex]:has-text('{texto}')",
            f"button:has-text('{texto}')",
            f"a:has-text('{texto}')",
            f"div:has-text('{texto}') >> nth=0",
        ]
        for sel in SELECTORES:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=400):
                    el.click()
                    return True
            except Exception as _e:
                if _es_fatal(_e): raise
                # Diagnóstico (19/10): si el clic del toggle falla (típico: un aviso lo tapa), deja un
                # incidente con captura. No cambia el flujo ni los timeouts. El hilo actual es el
                # MonitorThread de la cuenta.
                try:
                    _h = threading.current_thread()
                    _rm = getattr(_h, "_renderer_muerto", None)
                    if (hasattr(_h, "nombre") and not (_rm is not None and _rm.is_set())
                            and "has been closed" not in str(_e).lower()):
                        import incidentes
                        incidentes.registrar(_h, page, "clic_fallido", paso=f"tab_{texto}",
                                             selector=sel, error=_e, elemento=el)
                except Exception:
                    pass
                continue
        try:
            clicado = page.evaluate(f"""() => {{
                const patron = /{texto}/i;
                const candidatos = [
                    ...document.querySelectorAll('[role="tab"]'),
                    ...document.querySelectorAll('.mat-tab-label'),
                    ...document.querySelectorAll('.mat-mdc-tab'),
                    ...document.querySelectorAll('button'),
                    ...document.querySelectorAll('a'),
                ];
                for (const el of candidatos) {{
                    const txt = (el.textContent || '').trim();
                    if (patron.test(txt)) {{
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {{
                            el.click();
                            return true;
                        }}
                    }}
                }}
                return false;
            }}""")
            return bool(clicado)
        except Exception:
            return False

    def _toggle_movimientos(self, page) -> list | None:
        """Detalles → leer saldo → Movimientos → esperar API. Ciclo rápido (~3-5s)."""
        try:
            self._drenar_queue()
            self._click_tab_por_texto(page, "Detalles")
            time.sleep(0.5)
            saldo = self._leer_saldo_detalles(page)
            if saldo:
                if self.saldo_actual and self.saldo_actual not in ("—", None):
                    val_n = _parsear_pesos(saldo)
                    val_a = _parsear_pesos(self.saldo_actual)
                    if val_n is not None and val_a is not None and abs(val_n - val_a) >= 0.01:
                        print(f"[{self.nombre}] 🚨 Saldo: {self.saldo_actual} → {saldo}")
                self._saldo_en_detalles = saldo
            mov_ok = self._click_tab_por_texto(page, "Movimientos")
            if not mov_ok:
                print(f"[{self.nombre}] ⚠️ Toggle: no se encontró tab Movimientos")
                return None
            return self._esperar_api(timeout=8.0)
        except Exception as e:
            if _es_fatal(e): raise
            print(f"[{self.nombre}] ⚠️ _toggle_movimientos: {e}")
            return None

    def _click_diag(self, page, el, sel: str, paso: str):
        """el.click() EXACTAMENTE como antes (mismo timeout y mismo flujo: si falla, la
        excepción se relanza igual). Lo único nuevo: si falla por algo que no es un
        error fatal (típico: timeout porque una ventana tapa el botón), guarda un
        incidente con captura y datos EN ESE INSTANTE, cuando el obstáculo sigue ahí
        (ver incidentes.py)."""
        try:
            el.click()
        except Exception as _e:
            # Si el watchdog ya mató el Chrome no hay nada que fotografiar (incidente falso)
            _chrome_muerto = (self._renderer_muerto.is_set()
                              or "has been closed" in str(_e).lower())
            if not _es_fatal(_e) and not _chrome_muerto:
                try:
                    import incidentes
                    incidentes.registrar(self, page, "clic_fallido", paso=paso,
                                         selector=sel, error=_e, elemento=el)
                except Exception:
                    pass
                # ¿El clic falló porque Bancolombia cerró la sesión? Reconectar ya,
                # en vez de seguir chocando contra el aviso (una cuenta 15/10: ~2 min ciego).
                self._verificar_sesion_cerrada(page)
            raise

    def _verificar_sesion_cerrada(self, page):
        """Si Bancolombia muestra "Llevas un rato sin actividad / Cerramos la sesión"
        lanza la excepción de reconexión (la misma que el chequeo del inicio del ciclo
        en sesiones.py). No hace nada más: no cambia clics ni timeouts."""
        try:
            cerrada = page.locator("html").evaluate(
                """(raiz) => {
                    const t = [...document.querySelectorAll('.bc-modal-active')]
                        .map(e => e.innerText || '').join(' ').toLowerCase();
                    return t.includes('llevas un rato sin actividad') ||
                           t.includes('cerramos la sesi');
                }""", timeout=3000)
        except Exception:
            return
        if cerrada:
            print(f"[{self.nombre}] 🔒 Bancolombia cerró la sesión por inactividad — reconectando")
            raise Exception("Sesión cerrada por Bancolombia (inactividad) → reconectando")

    # ── Ciclo completo de navegación (llamado cada ciclo) ──────────
    def _incidente_sin_menu(self, page, paso: str):
        """Solo diagnóstico (no cambia el flujo): cuando el menú lateral no aparece, la página NO es la
        de siempre (aviso, bloqueo, pantalla intermedia...). Imprime URL, texto y botones y guarda un
        incidente con captura. Se limita a una vez por minuto y cuenta. (27/10: una cuenta en el nodo B
        quedó 20 min en "No se encontró 'Productos'" sin que se supiera qué mostraba la página.)"""
        try:
            ahora = time.time()
            if ahora - getattr(self, "_t_diag_sin_menu", 0) < 60:
                return
            self._t_diag_sin_menu = ahora
            info = {}
            try:
                info = page.locator("html").evaluate(
                    """(raiz) => ({
                        url: location.href, estado: document.readyState,
                        txt: document.body ? document.body.innerText : '',
                        btns: [...document.querySelectorAll('button, a[role="button"]')]
                              .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                              .map(b => (b.textContent || b.getAttribute('aria-label') || '').trim())
                              .filter(t => t).slice(0, 8)
                    })""", timeout=3000) or {}
            except Exception as _e:
                info = {"txt": f"(no se pudo leer la página: {_e})"}
            # La pantalla de bloqueo del WAF viene en un iframe: se lee también el texto de los marcos.
            marcos = []
            try:
                for _m in page.frames[1:4]:
                    try:
                        _t = " ".join((_m.locator("html").evaluate(
                            "(r) => document.body ? document.body.innerText : ''", timeout=1500) or "").split())[:120]
                        if _t:
                            marcos.append(_t)
                    except Exception:
                        pass
            except Exception:
                pass
            txt = " ".join((info.get("txt") or "").split())[:300]
            resumen = (f"{str(info.get('url', ''))[:90]} | estado={info.get('estado')} | texto: '{txt}' | "
                       f"botones: {info.get('btns')} | marcos: {marcos}")
            print(f"[{self.nombre}] 🔎 Sin menú lateral ({paso}) — {resumen}")
            import incidentes
            incidentes.registrar(self, page, "sin_menu", paso=paso, detalle=resumen[:600])
        except Exception:
            pass

    def _click_buscar(self, page) -> bool:
        """
        Ciclo: Inicio → Productos → Tus cuentas → Detalles → Movimientos.
        Este click en Movimientos dispara el XHR que _on_api_response captura.
        """
        if self._renderer_muerto.is_set():
            raise Exception("Renderer muerto — abortando _click_buscar")
        self._verificar_sesion_cerrada(page)
        # Actualizar latido al entrar: la navegación sidebar puede tardar > 60s
        # cuando Chrome está bajo presión (6 instancias simultáneas con paginación),
        # y el watchdog no debe disparar durante una navegación legítima.
        self._ultimo_latido = time.time()
        # Diagnóstico: segundos que toma cada paso de la navegación. Solo se
        # imprime (en la línea ✓); no cambia clics, esperas ni timeouts.
        _pasos: list = []
        self._nav_pasos = _pasos   # sesiones.py lo imprime si el ciclo falla
        _t_paso = [time.time()]
        def _marca(nombre: str):
            ahora = time.time()
            _pasos.append(f"{nombre} {ahora - _t_paso[0]:.1f}")
            _t_paso[0] = ahora
        try:
            # 1. Ir al Inicio
            inicio_ok = False
            for sel in [
                "a[href*='/home']", "[routerLink='/home']",
                "a:has-text('Inicio')", "mat-list-item:has-text('Inicio')",
                "span:has-text('Inicio')",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=500):
                        self._click_diag(page, el, sel, "inicio"); inicio_ok = True; break
                except Exception as _e:
                    if _es_fatal(_e): raise
                    continue
            if inicio_ok:
                time.sleep(0.1)
            _marca("inicio")

            # Cerrar coach-marks/overlays de Bancolombia antes de buscar Productos.
            # El modal "¿Olvidaste compartir el comprobante?" aparece al llegar a
            # movimientos y puede bloquear la sidebar en el ciclo siguiente.
            # Lo cerramos aquí también (ya se llama antes de _click_buscar, pero
            # Chrome puede tener el modal en animación cuando se inicia el ciclo).
            self._cerrar_modales(page)
            _marca("modales")

            # 2. Click Productos
            # Si el submenú de Productos YA está abierto (el arranque lo abrió y no llegó a entrar), pulsar
            # Productos otra vez lo CIERRA: "Tus cuentas" nunca aparece y el ciclo se pierde (dos cuentas 30/10:
            # tuscuentas 5.3 s + carga 12.1 s y "No se encontró tab Movimientos"). Con Inicio el menú se
            # reinicia solo; sin Inicio no, así que se mira antes de pulsar.
            _submenu_abierto = False
            try:
                _submenu_abierto = page.locator("a:has-text('Tus cuentas')").first.is_visible()
            except Exception as _e:
                if _es_fatal(_e): raise
            prod_ok = _submenu_abierto
            for sel in ([] if _submenu_abierto else [
                "a:has-text('Productos')", "mat-list-item:has-text('Productos')",
                "span:has-text('Productos')", "li:has-text('Productos') a",
            ]):
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        self._click_diag(page, el, sel, "productos"); prod_ok = True; break
                except Exception as _e:
                    if _es_fatal(_e): raise
                    continue

            if not prod_ok:
                # Un intento más después de cerrar modales por si el primer intento
                # topó con el overlay justo durante la animación de entrada.
                self._cerrar_modales(page)
                time.sleep(0.3)
                for sel in [
                    "a:has-text('Productos')", "mat-list-item:has-text('Productos')",
                    "span:has-text('Productos')", "li:has-text('Productos') a",
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1500):
                            self._click_diag(page, el, sel, "productos_reintento"); prod_ok = True; break
                    except Exception as _e:
                        if _es_fatal(_e): raise
                        continue

            if not prod_ok:
                print(f"[{self.nombre}] ⚠️ No se encontró 'Productos'")
                self._incidente_sin_menu(page, "productos")
                self._fallos_seguidos += 1
                if self._fallos_seguidos >= 5:
                    self._fallos_seguidos = 0
                    raise Exception("No se encuentra el sidebar × 5")
                return False
            _marca("productos")

            # 3. Esperar dropdown y click "Tus cuentas"
            _tc_visible = False
            try:
                page.wait_for_selector(
                    "a:has-text('Tus cuentas'), span:has-text('Tus cuentas')",
                    timeout=5000
                )
                _tc_visible = True
            except Exception:
                pass
            if not _tc_visible:
                # El submenú no se abrió (el clic en Productos no lo abrió, o lo cerró): un segundo clic lo abre.
                # Sin esto el tercer selector (":has-text") "hace clic" en cualquier contenedor, da ✓ falso y se
                # pierden 12 s esperando una página de cuenta que nunca carga.
                print(f"[{self.nombre}] ↻ 'Tus cuentas' no apareció tras Productos — se pulsa Productos otra vez")
                for sel in [
                    "a:has-text('Productos')", "mat-list-item:has-text('Productos')",
                    "span:has-text('Productos')", "li:has-text('Productos') a",
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1000):
                            self._click_diag(page, el, sel, "productos_reabrir"); break
                    except Exception as _e:
                        if _es_fatal(_e): raise
                        continue
                try:
                    page.wait_for_selector(
                        "a:has-text('Tus cuentas'), span:has-text('Tus cuentas')",
                        timeout=5000
                    )
                except Exception:
                    pass
            time.sleep(0.2)

            cuenta_ok = False
            for sel in [
                "a:has-text('Tus cuentas')",
                "span:has-text('Tus cuentas')",
                ":has-text('Tus cuentas')",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        self._click_diag(page, el, sel, "tuscuentas"); cuenta_ok = True; break
                except Exception as _e:
                    if _es_fatal(_e): raise
                    continue

            if not cuenta_ok:
                print(f"[{self.nombre}] ⚠️ No se encontró 'Tus cuentas'")
                self._incidente_sin_menu(page, "tuscuentas")
                self._fallos_seguidos += 1
                if self._fallos_seguidos >= 5:
                    self._fallos_seguidos = 0
                    raise Exception("Sin 'Tus cuentas' × 5")
                return False

            self._fallos_seguidos = 0
            _marca("tuscuentas")

            # 4. Esperar carga de la página de cuenta
            try:
                page.wait_for_url(
                    lambda u: "/detalles-y-movimientos/" in u,
                    timeout=12000
                )
            except Exception:
                pass
            time.sleep(0.1)
            _marca("carga")

            # 5. Leer saldo en Detalles
            saldo = self._leer_saldo_detalles(page)
            _marca("saldo")
            if saldo:
                if self.saldo_actual and self.saldo_actual not in ("—", None):
                    val_n = _parsear_pesos(saldo)
                    val_a = _parsear_pesos(self.saldo_actual)
                    if val_n is not None and val_a is not None and abs(val_n - val_a) >= 0.01:
                        print(f"[{self.nombre}] 🚨 Saldo: {self.saldo_actual} → {saldo}")
                self._saldo_en_detalles = saldo

            # 6. Click Movimientos → dispara XHR
            mov_ok = self._click_tab_por_texto(page, "Movimientos")
            if not mov_ok:
                print(f"[{self.nombre}] ⚠️ No se encontró tab Movimientos")
                self._incidente_sin_menu(page, "tab_movimientos")
            else:
                try:
                    td_pre = page.evaluate(
                        "() => document.querySelectorAll('td').length"
                    )
                    page.wait_for_function(
                        f"() => document.querySelectorAll('td').length !== {td_pre}",
                        timeout=3000
                    )
                except Exception:
                    pass
                _marca("movs")
                print(f"[{self.nombre}] 🔄 Inicio→Productos→TusCuentas→Detalles→Movimientos ✓"
                      f"  [{' | '.join(_pasos)}]")

            return True

        except Exception as e:
            if _es_fatal(e): raise
            print(f"[{self.nombre}] ⚠️ _click_buscar: {e}")
            return False

    # ── Saldo y modales ────────────────────────────────────────────
    def _leer_saldo_detalles(self, page) -> str | None:
        try:
            try:
                page.wait_for_function(
                    r"() => /\$\s*[\d\.]{3,}/.test(document.body?.innerText || '')",
                    timeout=3000
                )
            except Exception:
                pass
            saldo = extraer_saldo(page)
            if not saldo:
                time.sleep(0.6)
                saldo = extraer_saldo(page)
            return saldo
        except Exception:
            return None

    def _cerrar_modales(self, page):
        """Cierra avisos de Bancolombia. El tutorial "¿Olvidaste compartir el comprobante?" tiene
        VARIOS pasos ("Siguiente" … "Entendido") y cada pasada de _cerrar_modales_una solo
        pulsa UN botón: antes, el siguiente paso quedaba tapando el menú (overlay
        bc-coach-mark-overlay) hasta la pasada siguiente, y mientras tanto cada clic de la
        navegación esperaba 20 s (una cuenta 19/10: hasta 123 s de navegación y watchdog a los 64 s).
        Ahora, si tras pulsar queda el overlay, se repite (máx. 5 veces, acotado)."""
        r = self._cerrar_modales_una(page)
        if r in ('closed', 'closed_coachmark', 'closed_overlay'):
            for _ in range(5):
                try:
                    sigue = page.locator("html").evaluate(
                        """(raiz) => { const o = document.querySelector('.bc-coach-mark-overlay');
                                       if (!o) return false; const b = o.getBoundingClientRect();
                                       return b.width > 0 && b.height > 0; }""", timeout=1500)
                except Exception:
                    break
                if not sigue:
                    break
                if self._cerrar_modales_una(page) not in ('closed', 'closed_coachmark', 'closed_overlay'):
                    break   # no hay nada más que el sistema sepa pulsar
        return 'kept_session' if r == 'kept_session' else None

    def _cerrar_modales_una(self, page):
        try:
            resultado = page.evaluate("""() => {
                const re = /entendido|aceptar|continuar|cerrar|close|listo|ok/i;
                const bodyTxtLow = (document.body?.innerText || '').toLowerCase();

                // Pase 0 (PRIORITARIO): "¿Quieres cerrar sesión?" — Bancolombia muestra
                // este diálogo por inactividad. SIEMPRE clickear "No" para mantener la
                // sesión viva. Nunca dejar que el regex genérico toque "Sí".
                if (bodyTxtLow.includes('quieres cerrar sesi')) {
                    for (const btn of [...document.querySelectorAll('button, a[role="button"]')]) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const txt = (btn.textContent || btn.getAttribute('aria-label') || '')
                                        .trim().toLowerCase();
                        if (txt === 'no') { btn.click(); return 'kept_session'; }
                    }
                }

                // Pase 1: botones dentro de contenedores de diálogo conocidos, incluyendo
                // bc-coach-mark-container que usa Bancolombia para tooltips/popups guiados
                // (ej. "¿Olvidaste compartir el comprobante?").
                // 15/10: NUNCA pulsar el botón "CERRAR SESIÓN" del menú lateral (bc-menu lleva
                // la clase bc-coach-mark-target, así que este pase lo tomaba por un diálogo y
                // abría nosotros mismos el "¿Quieres cerrar sesión?"; confirmado en el log de una cuenta).
                const esLogout = (b, t) => /cerrar sesi/i.test(t) || !!b.closest('bc-menu, #svp-menu');
                for (const btn of [...document.querySelectorAll('button, a[role="button"]')]) {
                    const rect = btn.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const txt = (btn.textContent || btn.getAttribute('aria-label') || '').trim();
                    if (esLogout(btn, txt)) continue;
                    if (re.test(txt) && btn.closest(
                        'mat-dialog-container,bc-modal,[role="dialog"],' +
                        '[class*="modal"],[class*="overlay"],' +
                        '[class*="coach-mark"],[class*="coachmark"]'
                    )) {
                        // Diagnóstico (15/10): qué botón se pulsa y dentro de qué. Solo informa.
                        const cont = btn.closest('mat-dialog-container,bc-modal,[role="dialog"],' +
                            '[class*="modal"],[class*="overlay"],[class*="coach-mark"],[class*="coachmark"]');
                        const detalle = "botón '" + txt.slice(0, 30) + "' (" + btn.tagName +
                            (btn.getAttribute('aria-label') ? ' aria-label=' + btn.getAttribute('aria-label') : '') +
                            ') dentro de <' + (cont ? cont.tagName.toLowerCase() : '?') +
                            (cont && cont.id ? '#' + cont.id : '') + ' class="' +
                            ((cont && cont.className && cont.className.toString()) || '').slice(0, 60) + '">' +
                            (btn.closest('bc-menu, #svp-menu') ? ' [EN EL MENÚ LATERAL]' : '');
                        btn.click();
                        return 'closed:' + detalle;
                    }
                }
                // Pase 2: coach-mark de Bancolombia por ID de botón específico
                // (id="depositDetailAndMovement-bottom-next-step-*") — cubre casos donde
                // el contenedor no sea detectado por el pase 1.
                const btnCoach = document.querySelector(
                    'button[id*="depositDetailAndMovement-bottom-next-step"],' +
                    'button[id*="bottom-next-step"]'
                );
                if (btnCoach) {
                    const r = btnCoach.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        btnCoach.click();
                        return 'closed_coachmark';
                    }
                }
                // Pase 3: cualquier overlay con texto de comprobante visible
                const bodyTxt = (document.body?.innerText || '').toLowerCase();
                if (bodyTxt.includes('olvidaste compartir')) {
                    for (const btn of [...document.querySelectorAll('button, a[role="button"]')]) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const txt = (btn.textContent || btn.getAttribute('aria-label') || '').trim();
                        if (esLogout(btn, txt)) continue;
                        if (re.test(txt)) { btn.click(); return 'closed_overlay'; }
                    }
                }
                return null;
            }""")
            if isinstance(resultado, str) and resultado.startswith('closed:'):
                print(f"[{self.nombre}] 🗑️  Modal cerrado (Pase 1): {resultado[7:]}")
                resultado = 'closed'
            if resultado == 'kept_session':
                print(f"[{self.nombre}] 🛡️  '¿Cerrar sesión?' detectado — clic en NO para mantener sesión")
                time.sleep(0.5)
                # Navegar a home para que el ciclo siguiente pueda hacer
                # la navegación completa Inicio→Productos→Movimientos desde
                # un estado conocido. Sin esto el toggle Detalles→Movimientos
                # falla porque la página queda en /home tras cerrar el modal.
                try:
                    for _sel in [
                        "a[href*='/home']", "[routerLink='/home']",
                        "a:has-text('Inicio')", "mat-list-item:has-text('Inicio')",
                        "span:has-text('Inicio')",
                    ]:
                        try:
                            _el = page.locator(_sel).first
                            if _el.is_visible(timeout=600):
                                _el.click()
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                time.sleep(0.3)
                return resultado
            elif resultado and resultado != 'closed':
                print(f"[{self.nombre}] 🗑️  Modal Bancolombia cerrado ({resultado})")
            if resultado:
                time.sleep(0.5)
            return resultado
        except Exception:
            return None

    # ── DOM fallback (cuando el XHR no responde) ───────────────────
    @staticmethod
    def _scrape_dom_filas(page) -> list[str]:
        """Extrae filas de la tabla de movimientos desde el DOM."""
        try:
            filas_raw = page.evaluate(r"""() => {
                const resultado = [];
                const filas = document.querySelectorAll('table tr');
                filas.forEach(fila => {
                    const celdas = Array.from(fila.querySelectorAll('td'))
                        .map(c => (c.innerText || c.textContent || '').replace(/\s+/g, ' ').trim())
                        .filter(t => t.length > 0);
                    if (celdas.length >= 2) {
                        const texto = celdas.join(' | ');
                        if (texto.includes('$') || /[\d\.]{4,}/.test(texto))
                            resultado.push(texto);
                    }
                });
                if (resultado.length > 0) return resultado;

                const patron_dinero = /[$]?[\s ]?[\d\.]+[,\.]?\d{0,2}/;
                const patron_fecha  = /\d{1,2}\s+\w+\s+\d{4}/;
                const todos  = Array.from(document.querySelectorAll('*'));
                const vistos = new Set();
                todos.forEach(el => {
                    if (el.children.length > 0) return;
                    const texto = (el.innerText || el.textContent || '').trim();
                    if (texto.length < 5 || texto.length > 200) return;
                    if (!patron_dinero.test(texto)) return;
                    if (vistos.has(texto)) return;
                    vistos.add(texto);
                    let padre = el.parentElement;
                    for (let i = 0; i < 5 && padre; i++) {
                        const tf = (padre.innerText || padre.textContent || '').trim();
                        if (patron_fecha.test(tf) && patron_dinero.test(tf) && tf.length < 400) {
                            const limpio = tf.replace(/\n+/g, ' | ').replace(/\s{2,}/g, ' ');
                            if (!vistos.has(limpio)) {
                                vistos.add(limpio);
                                resultado.push(limpio);
                            }
                            break;
                        }
                        padre = padre.parentElement;
                    }
                });
                return resultado;
            }""")
            filas = [f for f in (filas_raw or []) if any(c.isdigit() for c in f)]
            return filas[:25]
        except Exception:
            return []

    def _fallback_dom(self, page, saldo_anterior: str, saldo_actual: str) -> tuple:
        """
        Fallback de detección por DOM scraping (Counter-based).
        Retorna (n_nuevas: int, filas_nuevas: list[str]).
        NO notifica — el caller decide.
        """
        try:
            self._click_tab_por_texto(page, "Movimientos")
            time.sleep(1.5)
            filas = self._scrape_dom_filas(page)
            if not filas:
                print(f"[{self.nombre}] ⚠️ DOM fallback: no se encontraron filas")
                return 0, []

            _sin_foto = not self._dom_filas_counter   # foto vacía → todo "parece" nuevo
            curr_counter: dict = {}
            for f in filas:
                curr_counter[f] = curr_counter.get(f, 0) + 1

            nuevas_filas: list = []
            for fila, count in curr_counter.items():
                known = self._dom_filas_counter.get(fila, 0)
                if count > known:
                    nuevas_filas.extend([fila] * (count - known))

            for fila, count in curr_counter.items():
                if count > self._dom_filas_counter.get(fila, 0):
                    self._dom_filas_counter[fila] = count

            # Solo filas que cuadran con el cambio de saldo (ver filtro_dom.py)
            _antes = len(nuevas_filas)
            nuevas_filas, _motivo = filtrar_filas_por_saldo(
                nuevas_filas, saldo_anterior, saldo_actual, _sin_foto)
            if len(nuevas_filas) < _antes:
                print(f"[{self.nombre}] 🧹 DOM: {_antes - len(nuevas_filas)} fila(s) "
                      f"histórica(s) descartada(s) — {_motivo or 'no cuadran con el saldo'}")

            if not nuevas_filas:
                print(f"[{self.nombre}] 🔍 DOM: sin filas nuevas "
                      f"(tx aún propagando en Bancolombia...)")
                return 0, []

            print(f"[{self.nombre}] 🔍 DOM: {len(nuevas_filas)} fila(s) nueva(s)")
            return len(nuevas_filas), nuevas_filas

        except Exception as e:
            if _es_fatal(e): raise
            print(f"[{self.nombre}] ⚠️ _fallback_dom: {e}")
            return 0, []

    # ── Paginación ─────────────────────────────────────────────────
    def _click_siguiente_pagina(self, page) -> bool:
        try:
            resultado = page.evaluate("""() => {
                const selectores = [
                    'a.bc-paginator-v2-next-link',
                    '[aria-label="Página siguiente"]',
                    'a[aria-label*="siguiente" i]',
                    '.bc-paginator-v2-next-link',
                ];
                for (const sel of selectores) {
                    const btn = document.querySelector(sel);
                    if (!btn) continue;
                    if (btn.getAttribute('aria-disabled') === 'true') return 'disabled';
                    btn.click();
                    return 'clicked';
                }
                return 'not_found';
            }""")
            return resultado == "clicked"
        except Exception as e:
            if _es_fatal(e): raise
            return False

    # 15 páginas = hasta 300 movimientos de hoy (antes 5 = 100). Solo se recorren
    # mientras las páginas anteriores estén llenas de movimientos de hoy.
    def _recopilar_txs_hoy(self, page, txs_pagina1: list, max_paginas: int = 15) -> list:
        """
        Si la página 1 está llena de txs de hoy, navega a páginas siguientes
        para capturar txs que Bancolombia relegó al fondo (ej. Nequi de bajo monto).
        """
        hoy  = datetime.now().strftime("%Y/%m/%d")
        todas = list(txs_pagina1)

        if len(txs_pagina1) < 20:
            return todas

        txs_hoy_p1 = [t for t in txs_pagina1 if t.get("transactionDate","") == hoy]
        if len(txs_hoy_p1) < len(txs_pagina1):
            return todas

        print(f"[{self.nombre}] 📄 Página 1 llena de txs de hoy — buscando más ({max_paginas})")

        for pag in range(2, max_paginas + 1):
            if self._renderer_muerto.is_set():
                print(f"[{self.nombre}] 💥 Renderer muerto — abortando paginación")
                break
            # Actualizar latido para que el watchdog no dispare durante la
            # paginación — cada página puede tardar hasta 8s de espera XHR.
            self._ultimo_latido = time.time()
            self._drenar_queue()
            if not self._click_siguiente_pagina(page):
                print(f"[{self.nombre}] 📄 Sin página {pag} (fin de paginación)")
                break

            txs_pag = self._esperar_api(timeout=8.0, page=page)
            if not txs_pag:
                print(f"[{self.nombre}] 📄 Sin respuesta XHR en página {pag}")
                break

            txs_hoy_pag = [t for t in txs_pag if t.get("transactionDate","") == hoy]
            todas.extend(txs_hoy_pag)
            print(f"[{self.nombre}] 📄 Página {pag}: "
                  f"{len(txs_pag)} txs, {len(txs_hoy_pag)} de hoy "
                  f"→ acumulado: {len(todas)}")

            if len(txs_hoy_pag) < len(txs_pag):
                break
            if len(txs_pag) < 20:
                break

        return todas

    # ── Snapshot diario (Movimientos del día) ──────────────────────
    DIARIO_DIR = os.path.join(os.path.dirname(__file__), "movimientos_diarios")

    def _guardar_snapshot_diario(self, txs_hoy: list):
        """Guarda las txs de hoy en movimientos_diarios/diario_<cuenta>.json."""
        try:
            os.makedirs(self.DIARIO_DIR, exist_ok=True)
            safe = re.sub(r"[^\w]", "_", self.nombre)
            ruta = os.path.join(self.DIARIO_DIR, f"diario_{safe}.json")
            hoy  = datetime.now().strftime("%Y/%m/%d")
            txs_filtradas = [t for t in txs_hoy if t.get("transactionDate","") == hoy]
            with open(ruta, "w", encoding="utf-8") as fh:
                json.dump({"fecha": hoy, "cuenta": self.nombre,
                           "txs": txs_filtradas}, fh, ensure_ascii=False, indent=2)
            print(f"[{self.nombre}] 💾 Snapshot diario: {len(txs_filtradas)} tx(s)")
        except Exception as e:
            print(f"[{self.nombre}] ⚠️ _guardar_snapshot_diario: {e}")

    # ── Reintentos (propagación tardía de Bancolombia) ─────────────
    def _reintentar_tx(self, page, delta: float, txs_base: list,
                       n_reintentos: int = 2, espera: float = 3.0) -> tuple:
        """
        Reintenta detectar la tx que causó un cambio de saldo.
        Bancolombia puede actualizar el saldo ANTES de propagar la tx a la API.
        Retorna (txs_finales, nuevas_txs).
        """
        txs_finales = txs_base
        fps_prev = frozenset(MovimientoMixin._fingerprint(t) for t in (txs_base or []))
        for _r in range(n_reintentos):
            time.sleep(espera)
            self._drenar_queue()
            try:
                self._click_tab_por_texto(page, "Detalles")
                time.sleep(0.3)
                time.sleep(0.4); self._cerrar_modales(page)   # tutorial de comprobantes (ver sesiones.py)
            except Exception:
                pass
            self._click_tab_por_texto(page, "Movimientos")
            _txs_r = self._esperar_api(timeout=8.0, page=page)
            if _txs_r:
                _txs_r = self._recopilar_txs_hoy(page, _txs_r)
                fps_curr = frozenset(MovimientoMixin._fingerprint(t) for t in _txs_r)
                if fps_curr == fps_prev:
                    print(f"[{self.nombre}] 🔍 Reintento {_r+1}: API sin cambios "
                          f"— tx fuera de ventana (Δ={delta:+.0f})")
                    txs_finales = _txs_r
                    break
                fps_prev = fps_curr
                txs_finales = _txs_r
                nuevas_r = [
                    t for t in self._detectar_nuevas(_txs_r)
                    if self._es_fecha_reciente(t.get("transactionDate", ""), dias=3)
                ]
                if nuevas_r:
                    print(f"[{self.nombre}] ✅ Tx detectada en reintento {_r+1}/{n_reintentos} "
                          f"(Δ={delta:+.0f})")
                    return txs_finales, nuevas_r
                print(f"[{self.nombre}] ⏳ Reintento {_r+1}/{n_reintentos}: "
                      f"API actualizó pero tx aún sin propagar (Δ={delta:+.0f})")
            else:
                print(f"[{self.nombre}] ⏳ Reintento {_r+1}/{n_reintentos}: "
                      f"sin respuesta API (Δ={delta:+.0f})")
        return txs_finales, []

    # ── Caché en disco ─────────────────────────────────────────────
    def _ruta_cache(self) -> str:
        safe = re.sub(r"[^\w]", "_", self.nombre)
        os.makedirs(CACHE_DIR, exist_ok=True)
        return os.path.join(CACHE_DIR, f"cache2_{safe}.json")

    def _cargar_cache(self):
        try:
            ruta = self._ruta_cache()
            if not os.path.exists(ruta):
                return
            with open(ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
            if datos.get("saldo_actual"):
                self.saldo_actual = datos["saldo_actual"]

            if datos.get("txs_fps_counter"):
                self.txs_conocidas = {k: int(v)
                                      for k, v in datos["txs_fps_counter"].items()}
            elif datos.get("txs_fps_set"):
                counter: dict = {}
                for fp_str in datos["txs_fps_set"]:
                    base = fp_str.rsplit("|#", 1)[0] if "|#" in fp_str else fp_str
                    counter[base] = counter.get(base, 0) + 1
                self.txs_conocidas = counter
            elif datos.get("txs_conocidas"):
                self.txs_conocidas = {fp: 1 for fp in datos["txs_conocidas"]}

            if datos.get("saldo_en_ultimo_movimiento"):
                self.saldo_en_ultimo_movimiento = datos["saldo_en_ultimo_movimiento"]
            if datos.get("last_snapshot"):
                self._last_snapshot = datos["last_snapshot"]
            ts    = datos.get("timestamp", "?")
            total = self._n_conocidas_total(self.txs_conocidas)
            print(f"[{self.nombre}] 💾 Caché [{ts}]: "
                  f"saldo={self.saldo_actual}, "
                  f"{len(self.txs_conocidas)} fps ({total} conteos), "
                  f"snapshot={len(self._last_snapshot)} txs")
        except Exception as e:
            print(f"[{self.nombre}] ⚠️ No se pudo cargar caché: {e}")

    def _guardar_cache(self):
        try:
            datos = {
                "saldo_actual":              self.saldo_actual,
                "txs_fps_counter":           self.txs_conocidas,
                "last_snapshot":             self._last_snapshot,
                "saldo_en_ultimo_movimiento": self.saldo_en_ultimo_movimiento,
                "timestamp":                 time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(self._ruta_cache(), "w", encoding="utf-8") as f:
                json.dump(datos, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[{self.nombre}] ⚠️ No se pudo guardar caché: {e}")
