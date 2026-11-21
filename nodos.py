# -*- coding: utf-8 -*-
# =====================================================================
# NODOS.PY — qué cuentas atiende ESTE PC (nodo A, nodo B, ...)
#
# Regla de oro: Pochonance DECIDE (enruta cada orden al nodo que le toca) y
# este módulo VERIFICA: si llega una orden que no es de este nodo, NO se abre
# nada, se avisa al dueño (OWNER_CHAT_ID) y se deja registro. Nunca en silencio.
#
# La verificación es por el ID de la cuenta EN POCHONANCE (account_cop.id), no
# por el nombre: Bitwarden sigue buscando por nombre, pero la pertenencia a un
# nodo se decide por ID. Cada orden de Pochonance debe traer {"cuenta", "id"}
# (y opcionalmente "nodo"); sin ID la orden se rechaza.
#
# Configuración en config.py (ver config_ejemplo.py):
#   NODO_ID          = "A"        # nombre de este PC
#   NODO_IDS_PROPIOS = "1-30"     # IDs de Pochonance que atiende (ej. B: "31-")
# Sin NODO_IDS_PROPIOS este nodo rechaza TODO a propósito (mejor parado que
# abriendo cuentas ajenas).
#
# cuentas_pochonance.txt (opcional, junto a este archivo): lista "ID nombre" de
# las cuentas, exportada de la base de datos. Sirve para (1) contrastar nombre
# e ID en cada orden y (2) saber el ID de una cuenta cuando se arranca A MANO
# desde el menú de Telegram, que no pasa por Pochonance.
# =====================================================================
import os
import re
import threading
import time
import unicodedata

try:
    import config as _config
except Exception:
    _config = None

NODO_ID = str(getattr(_config, "NODO_ID", "A") or "A").strip().upper()
TEXTO_RANGOS = str(getattr(_config, "NODO_IDS_PROPIOS", "") or "").strip()
ARCHIVO_MAPA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cuentas_pochonance.txt")
COOLDOWN_AVISO_S = 30 * 60   # el latido re-encola cada 10 s: no inundar el chat

AYUDA_MANUAL = ("Para arrancarla a mano hay que conocer su ID: agrégala a "
                "cuentas_pochonance.txt (ID y nombre) y verifica NODO_IDS_PROPIOS en config.py.")


def _parsear_rangos(texto: str) -> list:
    """'1-30', '31-', '31+', '5', '1-10,40,50-' -> [(desde, hasta|None)]."""
    rangos = []
    for parte in re.split(r"[,;\s]+", (texto or "").strip()):
        if not parte:
            continue
        m = re.fullmatch(r"(\d+)(?:(-)(\d*)|(\+))?", parte)
        if not m:
            raise ValueError(f"rango inválido: {parte!r}")
        desde = int(m.group(1))
        if m.group(2):
            hasta = int(m.group(3)) if m.group(3) else None
        elif m.group(4):
            hasta = None
        else:
            hasta = desde
        if hasta is not None and hasta < desde:
            raise ValueError(f"rango invertido: {parte!r}")
        rangos.append((desde, hasta))
    if not rangos:
        raise ValueError("vacío")
    return rangos


try:
    RANGOS = _parsear_rangos(TEXTO_RANGOS) if TEXTO_RANGOS else None
    ERROR_RANGOS = "" if RANGOS else "NODO_IDS_PROPIOS no está configurado en config.py"
except ValueError as _e:
    RANGOS = None
    ERROR_RANGOS = f"NODO_IDS_PROPIOS inválido en config.py ({_e})"


def id_es_mio(id_cuenta: int) -> bool:
    if not RANGOS:
        return False
    return any(d <= id_cuenta and (h is None or id_cuenta <= h) for d, h in RANGOS)


def norm(s) -> str:
    """minúsculas, sin tildes ni ñ, espacios colapsados (igual que el backend)."""
    s = unicodedata.normalize("NFKD", str(s or "")).lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


# ── Mapa ID <-> nombre (opcional) ──────────────────────────────────
_lock = threading.Lock()
_mapa_cache = {"mtime": None, "datos": {}}


def cargar_mapa() -> dict:
    """{id: nombre} de cuentas_pochonance.txt. Acepta '| 31 Nombre |' (salida de
    mysql, con o sin ' - bloqueada'), '31;Nombre' y '31<TAB>Nombre'. Ignora el resto."""
    try:
        mtime = os.path.getmtime(ARCHIVO_MAPA)
    except OSError:
        return {}
    with _lock:
        if _mapa_cache["mtime"] == mtime:
            return _mapa_cache["datos"]
    datos = {}
    try:
        with open(ARCHIVO_MAPA, encoding="utf-8-sig", errors="replace") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#"):
                    continue
                m = (re.match(r"^\|\s*(\d+)\s+(.+?)\s*\|?$", linea)
                     or re.match(r"^(\d+)\s*[;\t]\s*(.+)$", linea))
                if not m:
                    continue
                nombre = re.sub(r"\s*-\s*bloqueada\s*$", "", m.group(2), flags=re.I).strip()
                if nombre:
                    datos[int(m.group(1))] = nombre
    except Exception as e:
        print(f"⚠️ [Nodo {NODO_ID}] No se pudo leer cuentas_pochonance.txt: {e}")
        return {}
    with _lock:
        _mapa_cache.update(mtime=mtime, datos=datos)
    return datos


def _mismo_nombre(a: str, b: str) -> bool:
    """Compara nombres normalizados. Un '�' (tilde dañada al exportar de la consola)
    vale como UN carácter cualquiera."""
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    for patron, texto in ((na, nb), (nb, na)):
        if "�" in patron:
            rx = "".join("." if ch == "�" else re.escape(ch) for ch in patron)
            if re.fullmatch(rx, texto):
                return True
    return False


def id_de_nombre(nombre: str):
    """ID de Pochonance de una cuenta por nombre (solo con el mapa). None si no
    se conoce o es ambiguo."""
    coincidencias = [i for i, n in cargar_mapa().items() if _mismo_nombre(n, nombre)]
    return coincidencias[0] if len(coincidencias) == 1 else None


# ── Decisiones ─────────────────────────────────────────────────────
def decidir_pochonance(nombre: str, id_pochonance, nodo_backend=None) -> tuple:
    """Orden que llegó de Pochonance. (True, "") si ESTE nodo debe atenderla;
    (False, motivo) si no."""
    if not RANGOS:
        return False, ERROR_RANGOS
    if nodo_backend not in (None, "") and str(nodo_backend).strip().upper() != NODO_ID:
        return False, f"el backend la asignó al nodo {str(nodo_backend).strip().upper()}, no al {NODO_ID}"
    if id_pochonance in (None, "") or isinstance(id_pochonance, bool):
        # Backend que todavía no manda el ID (versión anterior al enrutamiento por nodo): se busca el ID por el
        # NOMBRE en el mapa local (cuentas_pochonance.txt). Si el nombre no está o es ambiguo, se rechaza.
        id_mapa = id_de_nombre(nombre)
        if id_mapa is None:
            return False, ("la orden no trae el ID de Pochonance y el nombre no está (o es ambiguo) en "
                           "cuentas_pochonance.txt")
        id_pochonance = id_mapa
    try:
        idn = int(id_pochonance)
    except (TypeError, ValueError):
        return False, f"el ID de la orden no es un número ({id_pochonance!r})"
    if not id_es_mio(idn):
        return False, f"la cuenta ID {idn} no pertenece al nodo {NODO_ID} (sus IDs: {TEXTO_RANGOS})"
    esperado = cargar_mapa().get(idn)
    if esperado and not _mismo_nombre(esperado, nombre):
        return False, (f"el nombre '{nombre}' no coincide con el ID {idn} del mapa local "
                       f"('{esperado}') — posible error de asignación")
    return True, ""


def decidir_manual(nombre: str) -> tuple:
    """Arranque a mano (menú de Telegram): no hay ID en la orden, se busca en el mapa."""
    if not RANGOS:
        return False, ERROR_RANGOS
    idn = id_de_nombre(nombre)
    if idn is None:
        return False, "no se conoce su ID de Pochonance (no está en cuentas_pochonance.txt o es ambiguo)"
    return decidir_pochonance(nombre, idn)


def filtrar_manual(cuentas: list) -> tuple:
    """Para el menú: cuentas es la lista de ítems de Bitwarden (dicts con 'name').
    Devuelve (visibles, cantidad_ocultas)."""
    visibles = [c for c in cuentas if decidir_manual(c.get("name", ""))[0]]
    return visibles, len(cuentas) - len(visibles)


# ── Avisos ─────────────────────────────────────────────────────────
_ultimos_avisos = {}


def avisar_rechazo(accion: str, nombre: str, id_pochonance, motivo: str) -> None:
    """Deja registro y avisa al dueño (solo OWNER, nunca a retiradores). Con
    enfriamiento por cuenta para no inundar si el backend la re-encola cada 10 s."""
    clave = (accion, norm(nombre), str(id_pochonance))
    ahora = time.time()
    with _lock:
        if ahora - _ultimos_avisos.get(clave, 0) < COOLDOWN_AVISO_S:
            return
        _ultimos_avisos[clave] = ahora
    texto = (f"⛔ Nodo {NODO_ID} RECHAZÓ la orden de {accion.upper()} de «{nombre}» "
             f"(ID {id_pochonance if id_pochonance not in (None, '') else '—'}): {motivo}. "
             f"No se abrió ni se tocó nada.")
    print(f"[Nodo {NODO_ID}] {texto}")
    try:
        from notificar import _enviar, OWNER_CHAT_ID
        _enviar(OWNER_CHAT_ID, texto)
    except Exception as e:
        print(f"[Nodo {NODO_ID}] ⚠️ No se pudo avisar al dueño: {e}")


def resumen() -> str:
    if not RANGOS:
        return (f"⛔ [Nodo {NODO_ID}] {ERROR_RANGOS} — este nodo RECHAZARÁ todas las órdenes "
                f"hasta que se configure.")
    return (f"🧭 [Nodo {NODO_ID}] atiende los IDs de Pochonance: {TEXTO_RANGOS} | "
            f"mapa local: {len(cargar_mapa())} cuenta(s)")
