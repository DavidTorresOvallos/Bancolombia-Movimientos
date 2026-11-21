# -*- coding: utf-8 -*-
# =====================================================================
# INCIDENTES.PY — aísla un fallo (clic que no entra, watchdog) con TODA su
# información para estudiarlo después, en una carpeta propia:
#
#   incidentes/<fecha_hora>_<cuenta>_<tipo>/
#       info.json         cuenta, nodo, tipo, paso, selector, error, tiempos de la
#                         navegación, URL, CPU/RAM del PC
#       pantalla.png      captura de la página EN ESE INSTANTE (el obstáculo sigue ahí)
#       diagnostico.json  qué hay encima del botón (elementFromPoint), ventanas/modales
#                         visibles con z-index y texto, heap de JS, nodos del DOM
#       sidebar.html      HTML del menú lateral y de las ventanas activas (recortado)
#       log_cuenta.txt    últimas líneas del log de esa cuenta y últimas generales
#
# Se llama desde: movimiento.py (_click_diag, cuando un clic de la navegación falla)
# y sesiones.py (watchdog, sin página: no se puede tocar Playwright desde otro hilo).
#
# NO cambia ningún timeout, clic ni flujo: solo mira y guarda. Nunca lanza
# excepciones. Máximo un incidente por (cuenta, tipo, paso) cada 2 minutos; se
# conservan los últimos 60 y 14 días.
#
# PRIVACIDAD: la captura puede mostrar saldos y nombres de titulares. Todo queda
# en este PC (incidentes/ está en .gitignore): no lo compartas tal cual.
#
# Ver los últimos:   python incidentes.py
# =====================================================================
import datetime
import glob
import json
import os
import re
import shutil
import sys
import threading
import time

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "incidentes")
MAX_CARPETAS = 60
DIAS = 14
ENFRIAMIENTO_S = 120

_lock = threading.Lock()
_ultimos = {}

# Se ejecuta DENTRO de la página (Locator.evaluate, con timeout). arg = {x, y} o null.
_JS_DIAG = """(raiz, arg) => {
  const corto = (s, n) => (s || '').toString().replace(/\\s+/g, ' ').trim().slice(0, n);
  const info = (e) => e ? {
      tag: e.tagName, id: e.id || '',
      clase: corto(e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className, 120),
      texto: corto(e.innerText || e.textContent, 160)
  } : null;
  const out = {
      url: location.href, titulo: document.title, estado: document.readyState,
      visible: document.visibilityState,
      nodos_dom: document.getElementsByTagName('*').length,
      heap_mb: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) : null,
      elemento_activo: info(document.activeElement)
  };
  if (arg && arg.x != null) {
      let e = document.elementFromPoint(arg.x, arg.y);
      out.en_el_punto_del_clic = info(e);
      const cadena = [];
      while (e && cadena.length < 6) { cadena.push(info(e)); e = e.parentElement; }
      out.cadena_en_el_punto = cadena;
  }
  const cand = document.querySelectorAll(
      'mat-dialog-container, bc-modal, [role="dialog"], [class*="modal"], [class*="overlay"], ' +
      '[class*="coach"], [class*="backdrop"], [class*="cdk-overlay"]');
  out.ventanas_visibles = [];
  for (const e of cand) {
      const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
      if (r.width <= 0 || r.height <= 0 || cs.visibility === 'hidden' || cs.display === 'none') continue;
      out.ventanas_visibles.push(Object.assign(info(e), {
          z: cs.zIndex, posicion: cs.position,
          rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]
      }));
      if (out.ventanas_visibles.length >= 12) break;
  }
  return out;
}"""


# HTML del menú lateral (bc-menu) y de las ventanas ACTIVAS (.bc-modal-active), cada uno
# recortado. Antes se tomaba la raíz de la app cortada a 30 000 caracteres y no llegaba al menú.
_JS_HTML = """(raiz) => {
  const cortar = (e, n) => e ? e.outerHTML.slice(0, n) : '(no encontrado)';
  const modales = [...document.querySelectorAll('.bc-modal-active')]
      .map(e => cortar(e, 6000)).join('\\n');
  return '<!-- MENU LATERAL -->\\n' + cortar(document.querySelector('bc-menu, #svp-menu'), 40000) +
         '\\n\\n<!-- VENTANAS ACTIVAS -->\\n' + (modales || '(ninguna)');
}"""


def _nombre_seguro(s: str) -> str:
    return (re.sub(r"[^\w\-]+", "_", s or "?").strip("_") or "x")[:40]


def _nodo() -> str:
    try:
        import config
        return str(getattr(config, "NODO_ID", "A") or "A").strip().upper()
    except Exception:
        return "A"


def _pc() -> dict:
    try:
        import psutil
        mem = psutil.virtual_memory()
        return {"cpu_pct": psutil.cpu_percent(interval=None), "ram_pct": mem.percent,
                "ram_usada_gb": round((mem.total - mem.available) / 1024 ** 3, 1)}
    except Exception:
        return {}


def _pila_hilo(monitor) -> list:
    """Últimas llamadas de Python del hilo de la cuenta (archivo:línea función → código).
    La última entrada es lo que está esperando ahora mismo. Nunca lanza excepciones."""
    try:
        import traceback
        frame = sys._current_frames().get(getattr(monitor, "ident", None))
        if frame is None:
            return ["(hilo no encontrado)"]
        return [f"{os.path.basename(f.filename)}:{f.lineno} {f.name} → {(f.line or '').strip()[:120]}"
                for f in traceback.extract_stack(frame)[-10:]]
    except Exception as e:
        return [f"(no se pudo leer la pila: {e})"]


def _escribir(ruta: str, contenido, binario=False) -> None:
    with open(ruta, "wb" if binario else "w", **({} if binario else {"encoding": "utf-8"})) as f:
        f.write(contenido)


def _limpiar() -> None:
    carpetas = sorted(p for p in glob.glob(os.path.join(DIR, "*")) if os.path.isdir(p))
    limite = time.time() - DIAS * 86400
    for p in list(carpetas):
        try:
            if os.path.getmtime(p) < limite:
                shutil.rmtree(p, ignore_errors=True)
                carpetas.remove(p)
        except OSError:
            pass
    for p in carpetas[:-MAX_CARPETAS]:
        shutil.rmtree(p, ignore_errors=True)


def registrar(monitor, page, tipo: str, paso: str = "", selector: str = "",
              error=None, elemento=None, detalle: str = ""):
    """Guarda un incidente. page=None: solo datos (sin captura). Devuelve la carpeta o None."""
    nombre = getattr(monitor, "nombre", "?")
    try:
        clave = (nombre, tipo, paso)
        ahora = time.time()
        with _lock:
            if ahora - _ultimos.get(clave, 0) < ENFRIAMIENTO_S:
                return None
            _ultimos[clave] = ahora

        sello = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = f"{sello}_{_nombre_seguro(nombre)}_{_nombre_seguro(tipo)}" + (f"_{_nombre_seguro(paso)}" if paso else "")
        carpeta, n = os.path.join(DIR, base), 1
        while os.path.exists(carpeta):          # nunca pisar los archivos de otro incidente
            n += 1
            carpeta = os.path.join(DIR, f"{base}_{n}")
        os.makedirs(carpeta)

        info = {
            "hora": datetime.datetime.now().isoformat(timespec="seconds"),
            "cuenta": nombre, "nodo": _nodo(), "tipo": tipo, "paso": paso,
            "selector": selector, "detalle": detalle,
            "error_tipo": type(error).__name__ if error is not None else "",
            "error": str(error)[:600] if error is not None else "",
            "nav_pasos": list(getattr(monitor, "_nav_pasos", []) or []),
            "segundos_desde_ultimo_latido": round(ahora - getattr(monitor, "_ultimo_latido", ahora), 1),
            "pc": _pc(),
        }
        # Dónde está BLOQUEADO el hilo de la cuenta en este instante (solo lectura: el
        # watchdog no puede tocar la página desde otro hilo, pero sí leer su pila de Python).
        if tipo == "watchdog":
            info["pila_hilo"] = _pila_hilo(monitor)

        diag = None
        if page is not None:
            punto = None
            if elemento is not None:
                try:
                    bb = elemento.bounding_box(timeout=1500)
                    if bb:
                        punto = {"x": bb["x"] + bb["width"] / 2, "y": bb["y"] + bb["height"] / 2}
                        info["elemento_rect"] = [round(bb["x"]), round(bb["y"]), round(bb["width"]), round(bb["height"])]
                except Exception:
                    pass
            pantalla_ok = False
            try:
                page.screenshot(path=os.path.join(carpeta, "pantalla.png"), timeout=4000)
                pantalla_ok = True
            except Exception as e:
                info["pantalla_error"] = str(e)[:200]
            # Si ni la captura salió, la página está congelada: no insistir con más llamadas.
            # Y la captura entera tiene un tope (no debe acercar al watchdog de 60 s).
            if pantalla_ok and time.time() - ahora < 6:
                try:
                    diag = page.locator("html").evaluate(_JS_DIAG, punto, timeout=3000)
                    info["url"] = diag.get("url", "")
                except Exception as e:
                    info["diagnostico_error"] = str(e)[:200]
                try:
                    html = page.locator("html").evaluate(_JS_HTML, timeout=2000)
                    _escribir(os.path.join(carpeta, "sidebar.html"), html)
                except Exception:
                    pass

        if diag is not None:
            _escribir(os.path.join(carpeta, "diagnostico.json"), json.dumps(diag, ensure_ascii=False, indent=2))
        try:
            import consola
            lineas = consola.ultimas_lineas(250, contiene=f"[{nombre.strip()}") or []
            generales = consola.ultimas_lineas(60)
            _escribir(os.path.join(carpeta, "log_cuenta.txt"),
                      f"=== ultimas lineas de {nombre} ===\n" + "\n".join(lineas) +
                      "\n\n=== ultimas lineas generales ===\n" + "\n".join(generales) + "\n")
        except Exception:
            pass
        _escribir(os.path.join(carpeta, "info.json"), json.dumps(info, ensure_ascii=False, indent=2))
        _limpiar()
        print(f"[{nombre}] 📸 Incidente guardado ({tipo}"
              f"{' / ' + paso if paso else ''}): incidentes/{os.path.basename(carpeta)}")
        return carpeta
    except Exception as e:
        try:
            print(f"[{nombre}] ⚠️ incidentes: no se pudo guardar ({e})")
        except Exception:
            pass
        return None


def listar(n: int = 20) -> None:
    carpetas = sorted((p for p in glob.glob(os.path.join(DIR, "*")) if os.path.isdir(p)), reverse=True)[:n]
    if not carpetas:
        print("No hay incidentes guardados.")
        return
    for p in carpetas:
        try:
            with open(os.path.join(p, "info.json"), encoding="utf-8") as f:
                d = json.load(f)
            ventanas = ""
            ruta_d = os.path.join(p, "diagnostico.json")
            if os.path.exists(ruta_d):
                with open(ruta_d, encoding="utf-8") as f:
                    v = json.load(f).get("ventanas_visibles") or []
                if v:
                    ventanas = "  | encima: " + "; ".join(
                        (x.get("texto") or x.get("clase") or x.get("tag") or "?")[:60] for x in v[:3])
            print(f"{os.path.basename(p)}\n    {d.get('tipo')} / {d.get('paso')}  {d.get('error_tipo')}  "
                  f"nav={' | '.join(d.get('nav_pasos', []))}{ventanas}")
        except Exception as e:
            print(f"{os.path.basename(p)}   (ilegible: {e})")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    listar()
