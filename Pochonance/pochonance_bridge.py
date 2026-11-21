# -*- coding: utf-8 -*-
# =====================================================================
# POCHONANCE_BRIDGE.PY
# Canal de comunicación entre el bot de movimientos y la plataforma
# Pochonance. Completamente aislado — no toca la lógica de Telegram.
#
# Si POCHONANCE_URL está vacío en config.py el bridge se desactiva y
# el sistema funciona exactamente igual que antes.
#
# Eventos que envía:
#   "movimiento"        → transacción detectada (XHR, DOM o reconexión)
#   "cambio_saldo"      → saldo cambió pero no se pudo identificar la tx
#   "conexion_exitosa"  → primer login+datos OK de una cuenta recién activada
#   "error_login"       → primer login fallido de una cuenta recién activada
#
# UBICACIÓN: vive en Movimientos/Pochonance/ junto con pochonance_activador.py
# (el otro script de conexión con la plataforma Pochonance). Lo importa
# notificar.py, que se queda en Movimientos/ (módulo núcleo compartido con
# iniciar.py y sesiones.py).
# =====================================================================
import os
import sys
import json
import threading
import time
import uuid
from datetime import datetime

# Bootstrap: Movimientos/ (el padre de esta carpeta Pochonance/) tiene que
# estar en sys.path para poder importar config.py sin importar desde dónde
# se ejecute/importe este módulo — evita que el try/except de abajo caiga
# en el fallback silencioso (bridge desactivado) solo por un import mal resuelto.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _requests = None
    _REQUESTS_OK = False
    print("[Pochonance] ⚠️  'requests' no instalado — bridge desactivado. "
          "Ejecuta: pip install requests --break-system-packages")

# ── Configuración desde config.py ─────────────────────────────────
try:
    from config import POCHONANCE_URL, POCHONANCE_API_KEY
except ImportError:
    POCHONANCE_URL    = ""
    POCHONANCE_API_KEY = ""

try:
    from config import TOKEN_CUENTAS_P2P
except ImportError:
    TOKEN_CUENTAS_P2P = ""

# FULL_NOTIF_IDS: mismos chat IDs que ya reciben el mensaje COMPLETO en el
# bot de Movimientos (ver notificar.py → _notificar_tx). Se reusa acá para
# que Cuentas P2P respete la misma regla: completo para FULL_NOTIF_IDS,
# resumido para el resto (a pedido del equipo, 31/08/2026).
try:
    from config import FULL_NOTIF_IDS
except ImportError:
    FULL_NOTIF_IDS = set()

# NotificarMixin trae _format_fecha/_format_monto — se reusan para que el
# bloque de "Últimos movimientos" que se manda a Pochonance (evento
# conexion_exitosa) salga con el MISMO formato exacto del primer mensaje
# que iniciar.py ya manda al chat de Movimientos (ver _esperar_y_notificar
# en iniciar.py), en vez de reimplementar el formato de fecha/monto acá.
#
# A PROPÓSITO no se importa acá arriba (import circular, bug encontrado el
# 31/08/2026): notificar.py importa de este mismo módulo (Pochonance.
# pochonance_bridge) cerca de la línea 33, ANTES de definir la clase
# NotificarMixin más abajo en el archivo. Si este módulo intenta
# "from notificar import NotificarMixin" en su propio import de arriba,
# Python ya está a mitad de cargar notificar.py (módulo parcialmente
# inicializado) y NotificarMixin todavía no existe ahí — el ImportError caía
# siempre en el fallback (NotificarMixin = None), así que el bloque de
# "Últimos movimientos" quedaba vacío y todo evento conexion_exitosa
# terminaba mostrando "Sin movimientos recientes." aunque sí hubiera
# transacciones. La importación se hace DENTRO de _formatear_movimientos_texto
# (ver abajo), en el momento en que de verdad se necesita — para entonces
# notificar.py ya terminó de cargar por completo y NotificarMixin ya existe.

# ── Constantes del bridge ─────────────────────────────────────────
_TIMEOUT_S       = 20   # segundos máximo por intento (Railway puede tardar ~15s en despertar)
_MAX_REINTENTOS  = 5    # más reintentos para cubrir cold starts y spikes de carga
_PAUSA_REINTENTO = 5    # espera BASE entre reintentos — se duplica con cada intento (backoff exponencial)

# ── Escenario 2 (25/09/2026) ────────────────────────────────────────
# Pochonance responder 200 OK al POST /evento solo confirma que RECIBIÓ el
# evento — no que ya le llegó al usuario por Telegram (esa entrega puede
# fallar internamente en el backend sin que se note en el HTTP). Estas
# constantes controlan el polling corto a GET /evento/{id}/estado que
# confirma la entrega de verdad; si se agota sin confirmación, se activa el
# mismo fallback directo del Escenario 1. Configurables en config.py si hace
# falta ajustarlas sin tocar este archivo.
try:
    from config import ACK_TIMEOUT_S
except ImportError:
    ACK_TIMEOUT_S = 30  # segundos totales esperando confirmación de entrega
try:
    from config import ACK_POLL_INTERVAL_S
except ImportError:
    ACK_POLL_INTERVAL_S = 2  # cada cuánto se pregunta por el estado

# ── Estado interno ────────────────────────────────────────────────
_bridge_activo: bool = bool(POCHONANCE_URL and _REQUESTS_OK)


def _simulacion_activa() -> bool:
    """True si el sistema corre en modo simulación (ver simulacion.py).
    En ese caso el bridge no envía NADA (ni a Railway ni a Cuentas P2P)."""
    try:
        import simulacion
        return simulacion.activo()
    except Exception:
        return False


def _fallback_telegram_directo(payload: dict):
    """
    Escenario 1 — fallback directo cuando Pochonance no está disponible.

    Usa el bot 'Cuentas P2P' (TOKEN_CUENTAS_P2P) para enviar la notificación
    directamente a los mismos chat_ids de chats_id.json, SIN pasar por Railway.

    Solo se activa para eventos 'movimiento' y 'cambio_saldo'.
    Los chats en FULL_NOTIF_IDS reciben el mensaje completo; el resto recibe
    el compacto — mismo criterio que ya aplica Bot Saldo en notificar.py.
    """
    if _simulacion_activa():
        return   # modo simulación: Cuentas P2P real no recibe nada
    if not TOKEN_CUENTAS_P2P or not _REQUESTS_OK:
        return

    evento  = payload.get("evento", "")
    cuenta  = payload.get("cuenta", "?")
    tipo    = payload.get("tipo", "entrada")
    es_sal  = tipo == "salida"
    emoji   = "🔴" if es_sal else "🟢"
    accion  = "Salió" if es_sal else "Entró"

    def _fmt(v: float) -> str:
        """Formatea un número como '$ 1.234.567' (mismo criterio que Bot Saldo)."""
        v   = abs(float(v))
        i   = int(v)
        d   = round((v - i) * 100)
        s   = f"{i:,}".replace(",", ".")
        return f"$ {s},{d:02d}" if d else f"$ {s}"

    if evento == "movimiento":
        monto      = _fmt(payload.get("monto", 0))
        desc       = (payload.get("descripcion") or "—").strip()
        desc_corta = (payload.get("descripcion_corta") or desc[:20]).strip()
        saldo_ant  = payload.get("saldo_anterior", "—")
        saldo_act  = payload.get("saldo_actual",   "—")
        ref        = (payload.get("referencia") or "").strip()
        fecha      = (payload.get("fecha_transaccion") or "").strip()

        partes_full = [
            f"{emoji} *{cuenta}*",
            f"💰 Saldo actual:   `{saldo_act}`",
            f"📊 Saldo anterior: `{saldo_ant}`",
            f"{'📤' if es_sal else '📥'} {accion}: `{monto}`",
            f"📝 {desc}",
        ]
        if fecha:
            partes_full.append(f"📅 {fecha}")
        if ref:
            partes_full.append(f"🔖 {ref}")
        msg_full  = "\n".join(partes_full)
        msg_corto = (
            f"{emoji} *{cuenta}*\n"
            f"{'📤' if es_sal else '📥'} {accion}: `{monto}`\n"
            f"💳 {desc_corta}"
        )

    elif evento == "cambio_saldo":
        delta     = _fmt(payload.get("delta", 0))
        saldo_ant = payload.get("saldo_anterior", "—")
        saldo_act = payload.get("saldo_actual",   "—")
        msg_full  = (
            f"{emoji} *{cuenta}* — cambio de saldo\n"
            f"💰 `{saldo_ant}` → `{saldo_act}`\n"
            f"{'📤' if es_sal else '📥'} Delta: `{delta}`"
        )
        msg_corto = msg_full   # para cambio de saldo no hay versión más compacta

    else:
        return  # conexion_exitosa / error_login no necesitan fallback

    # Leer chat_ids y FULL_NOTIF_IDS
    try:
        from notificar import _leer_chat_ids
        chat_ids = _leer_chat_ids()
    except Exception as e:
        print(f"[Pochonance/fallback] ⚠️  No se pudo leer chat_ids: {e}")
        return

    tg_url   = f"https://api.telegram.org/bot{TOKEN_CUENTAS_P2P}/sendMessage"
    enviados = 0
    for cid in chat_ids:
        texto = msg_full if cid in FULL_NOTIF_IDS else msg_corto
        try:
            r = _requests.post(
                tg_url,
                json={"chat_id": cid, "text": texto, "parse_mode": "Markdown"},
                timeout=10,
            )
            if r.status_code < 300:
                enviados += 1
            else:
                print(f"[Pochonance/fallback] ⚠️  Chat {cid}: HTTP {r.status_code}")
        except Exception as e:
            print(f"[Pochonance/fallback] ⚠️  Chat {cid}: {e}")

    print(f"[Pochonance/fallback] 🚨 Fallback directo Cuentas P2P: "
          f"{enviados}/{len(chat_ids)} chats notificados para '{cuenta}'")


def _esta_activo() -> bool:
    """Retorna True si el bridge está configurado y listo para enviar."""
    return _bridge_activo


def _url_estado(evento_id: str) -> str:
    """
    Construye la URL de GET /evento/{id}/estado a partir de POCHONANCE_URL
    (que apunta a POST /movimientos/evento). Si por algún motivo POCHONANCE_URL
    no termina en "/evento" (config atípica), se arma igual sobre la base
    completa — el backend expone ambas rutas bajo el mismo /movimientos/**.
    """
    base = POCHONANCE_URL[:-len("/evento")] if POCHONANCE_URL.endswith("/evento") else POCHONANCE_URL
    return f"{base}/evento/{evento_id}/estado"


def _confirmar_entrega(evento_id: str, headers: dict) -> bool:
    """
    Escenario 2: hace polling corto a GET /evento/{id}/estado durante
    ACK_TIMEOUT_S segundos, preguntando cada ACK_POLL_INTERVAL_S si el evento
    ya se le entregó al usuario por Telegram.

    Retorna True apenas el backend confirma "entregado: true". Retorna False
    en cuanto el backend confirma explícitamente "entregado: false" (falla
    real, no hace falta esperar el resto del timer) o si se agota el tiempo
    sin ninguna confirmación (204 todo el tiempo, o el backend no responde).
    """
    url_estado = _url_estado(evento_id)
    limite = time.monotonic() + ACK_TIMEOUT_S
    while time.monotonic() < limite:
        try:
            r = _requests.get(url_estado, headers=headers, timeout=10)
            if r.status_code == 200:
                entregado = bool(r.json().get("entregado"))
                if entregado:
                    return True
                # entregado: false explícito — el backend ya terminó de
                # procesar y SÍ hubo una falla real, no hace falta seguir
                # esperando el resto del timer.
                print(f"[Pochonance/ack] ⚠️  Evento {evento_id}: backend confirmó "
                      f"que NO se entregó — activando fallback sin esperar más.")
                return False
            # 204 (o cualquier otra cosa): todavía pendiente, seguir esperando.
        except Exception as e:
            print(f"[Pochonance/ack] ⚠️  Error consultando estado de {evento_id}: {e}")
        time.sleep(ACK_POLL_INTERVAL_S)

    print(f"[Pochonance/ack] ⏱️  Evento {evento_id}: sin confirmación de entrega "
          f"tras {ACK_TIMEOUT_S}s — activando fallback.")
    return False


def _enviar_a_pochonance(payload: dict):
    """
    Envía el payload JSON al endpoint de Pochonance.
    Corre en un hilo daemon → nunca bloquea el hilo principal.
    Reintenta hasta _MAX_REINTENTOS veces ante errores de red.
    """
    if _simulacion_activa():
        return   # modo simulación: cero POST a Railway
    if not _bridge_activo:
        return

    # A pedido de el equipo (31/08/2026): el bot "Cuentas P2P" debe avisarle a
    # los MISMOS chats que ya usa el sistema de Movimientos (chats_id.json),
    # no a una lista fija aparte en application-prod.properties. Se manda el
    # chat_ids ACTUAL (siempre fresco, se lee del JSON en cada evento) como
    # parte del payload; el backend lo usa si viene, y si no (p.ej. una
    # prueba manual por curl/PowerShell sin este campo) cae de vuelta a su
    # lista fija de app.cuentasp2p.chats-confiables — no rompe nada existente.
    # Import diferido: mismo motivo que NotificarMixin más abajo (ciclo con
    # notificar.py si se importa arriba del archivo).
    try:
        from notificar import _leer_chat_ids
        payload["chats_confiables"] = ",".join(str(c) for c in _leer_chat_ids())
    except Exception as e:
        print(f"[Pochonance] ⚠️  No se pudo leer chats_id.json para chats_confiables: {e}")

    # Subconjunto de chats_confiables que debe recibir el mensaje COMPLETO
    # (igual que en notificar.py → _notificar_tx). El backend usa esto para
    # mandarle a cada chat la versión completa o la resumida — el mismo
    # criterio que ya aplica el bot de Movimientos, para que a esa persona
    # que le gusta recibir el mensaje resumido no le cambie nada.
    if FULL_NOTIF_IDS:
        payload["chats_full"] = ",".join(str(c) for c in FULL_NOTIF_IDS)

    # Escenario 2 (25/09/2026): UUID único por evento, para poder preguntarle
    # después al backend "¿esto ya se entregó?" vía GET /evento/{id}/estado.
    evento_id = str(uuid.uuid4())
    payload["evento_id"] = evento_id

    url     = POCHONANCE_URL
    headers = {
        "Content-Type":  "application/json",
        # El backend (MovimientosBridgeController) valida la key leyendo el
        # header "X-Bot-Api-Key" (ver _errorSiApiKeyInvalida) -- NO "Authorization:
        # Bearer ...". Con el header equivocado, apiKeyRecibida llega null y el
        # backend rechaza TODO con 401, aunque POCHONANCE_API_KEY esté bien puesta
        # en config.py (bug encontrado el 31/08/2026: todos los eventos
        # conexion_exitosa/error_login daban 401 "API key inválida o ausente").
        "X-Bot-Api-Key": POCHONANCE_API_KEY or "",
        "X-Source":      "bancolombia-bot",
    }

    def _post():
        import random
        # Jitter inicial: distribuye los POSTs en el tiempo cuando muchas cuentas
        # reconectan al mismo tiempo (p.ej. arranque del sistema) y Railway recibe
        # un spike de solicitudes simultáneas que lo hace colapsar.
        # movimiento/cambio_saldo = distribución amplia (hasta 3s); eventos de
        # control (conexion_exitosa, error_login) van casi de inmediato (hasta 0.5s).
        evento = payload.get("evento", "")
        jitter = random.uniform(0, 0.5) if evento in ("conexion_exitosa", "error_login") \
                 else random.uniform(0, 3)
        time.sleep(jitter)

        _pochonance_ok = False
        for intento in range(1, _MAX_REINTENTOS + 1):
            try:
                resp = _requests.post(
                    url,
                    data=json.dumps(payload, ensure_ascii=False),
                    headers=headers,
                    timeout=_TIMEOUT_S,
                )
                if resp.status_code < 300:
                    print(f"[Pochonance] ✅ {evento} → {resp.status_code} "
                          f"(evento_id={evento_id}) — esperando confirmación de entrega…")
                    _pochonance_ok = True

                    # Escenario 2: el 200 OK solo dice que Pochonance RECIBIÓ el
                    # evento, no que ya le llegó al usuario por Telegram — solo
                    # los eventos "movimiento"/"cambio_saldo" tienen fallback
                    # real (ver _fallback_telegram_directo), así que solo para
                    # esos vale la pena esperar la confirmación.
                    if evento in ("movimiento", "cambio_saldo"):
                        if _confirmar_entrega(evento_id, headers):
                            print(f"[Pochonance] ✅ Entrega confirmada para evento_id={evento_id}")
                        else:
                            print(f"[Pochonance] ❌ Entrega NO confirmada para evento_id={evento_id} "
                                  f"— activando fallback directo (Escenario 2)")
                            _fallback_telegram_directo(payload)
                    return
                else:
                    # Pochonance respondió con error: no retryamos (ya respondió),
                    # pero tampoco entregó la notificación → activa fallback.
                    print(f"[Pochonance] ⚠️  {evento} → HTTP {resp.status_code}: "
                          f"{resp.text[:120]}")
                    break
            except Exception as e:
                print(f"[Pochonance] ⚠️  Intento {intento}/{_MAX_REINTENTOS} fallido: {e}")
                if intento < _MAX_REINTENTOS:
                    # Backoff exponencial: 5s, 10s, 20s, 40s — cubre el cold start
                    # de Railway (~15-30s) y los picos de carga por reconexión masiva.
                    pausa = _PAUSA_REINTENTO * (2 ** (intento - 1))
                    print(f"[Pochonance]    Esperando {pausa}s antes del intento {intento+1}…")
                    time.sleep(pausa)

        # Pochonance no disponible — Escenario 1: notificación directa
        print(f"[Pochonance] ❌ No se pudo enviar evento '{evento}' "
              f"para '{payload.get('cuenta', '?')}' — activando fallback directo")
        _fallback_telegram_directo(payload)

    threading.Thread(target=_post, daemon=True).start()


# ══════════════════════════════════════════════════════════════════
# EVENTOS PÚBLICOS
# Llamados desde notificar.py sin modificar la lógica de Telegram.
# ══════════════════════════════════════════════════════════════════

def bridge_movimiento(
    cuenta: str,
    tx: dict,
    saldo_anterior: str,
    saldo_actual: str,
    desc_corta: str = "",
):
    """
    Notifica a Pochonance que se detectó una transacción.

    Parámetros:
        cuenta         → nombre de la cuenta (ej: "Juan Pérez")
        tx             → dict con los campos del API de Bancolombia
        saldo_anterior → saldo antes de la tx (ej: "$ 1.656.092")
        saldo_actual   → saldo después de la tx (ej: "$ 1.763.292")
        desc_corta     → etiqueta breve del canal (ej: "NEQUI", "PSE")
    """
    if not _bridge_activo:
        return

    amount    = float(tx.get("amount", 0))
    fecha_raw = tx.get("transactionDate", "")  # formato "2026/08/25"
    fecha_iso = fecha_raw.replace("/", "-") if fecha_raw else ""

    payload = {
        "evento":          "movimiento",
        "cuenta":          cuenta,
        "tipo":            "salida" if amount < 0 else "entrada",
        "monto":           abs(amount),
        "saldo_anterior":  saldo_anterior,
        "saldo_actual":    saldo_actual,
        "descripcion":     (tx.get("description") or "").strip(),
        "descripcion_corta": desc_corta,
        "referencia":      (tx.get("reference1") or "").strip(),
        "fecha_transaccion": fecha_iso,
        "timestamp":       datetime.now().isoformat(),
    }
    _enviar_a_pochonance(payload)


def bridge_cambio_saldo(
    cuenta: str,
    saldo_anterior: str,
    saldo_actual: str,
    delta: float,
):
    """
    Notifica a Pochonance que el saldo cambió sin poder identificar
    la transacción exacta (fingerprint sin coincidencia o DOM fallback).

    Parámetros:
        cuenta         → nombre de la cuenta
        saldo_anterior → saldo antes del cambio
        saldo_actual   → saldo después del cambio
        delta          → diferencia numérica (positivo = entrada, negativo = salida)
    """
    if not _bridge_activo:
        return

    payload = {
        "evento":         "cambio_saldo",
        "cuenta":         cuenta,
        "tipo":           "salida" if delta < 0 else "entrada",
        "delta":          abs(delta),
        "saldo_anterior": saldo_anterior,
        "saldo_actual":   saldo_actual,
        "timestamp":      datetime.now().isoformat(),
    }
    _enviar_a_pochonance(payload)


def _formatear_movimientos_texto(txs: list) -> str:
    """Arma el bloque '📋 Movimientos de hoy:\n1. 🔴 ...' con TODOS los
    movimientos del día, SIN truncar (18/09/2026, corregido a pedido de
    Nota: la versión anterior cortaba a 2500 caracteres y avisaba "... y N
    más" en vez de mandarlos — perdía movimientos reales cuando el día tenía
    muchas transacciones). Si el texto queda muy largo para un solo mensaje
    de Telegram, es el BACKEND quien lo parte en varios envíos (ver
    MovimientosBridgeServiceImpl.partirEnBloques) — acá nunca se descarta
    nada, igual que ya hace iniciar.py para el bot de Saldo.
    Se arma acá (no en el backend) para que el bot 'Cuentas P2P' muestre el
    mismo formato exacto que ya conocen del bot de Movimientos."""
    if not txs:
        return ""
    # Import diferido a propósito — ver el comentario largo arriba, cerca de
    # POCHONANCE_URL: importar NotificarMixin al cargar este módulo cae en un
    # ciclo con notificar.py y siempre queda None. Acá, en cambio, ya se
    # ejecuta con notificar.py completamente cargado.
    try:
        from notificar import NotificarMixin
    except ImportError:
        return ""
    # 18/09/2026: la fecha ya no va en cada línea (todas son de HOY, era
    # redundante repetirla) — va una sola vez en el encabezado.
    # Ordenar de mayor a menor monto absoluto (solo para el mensaje,
    # no modifica la lista original ni ningún estado interno).
    txs_ordenadas = sorted(txs, key=lambda t: abs(float(t.get("amount", 0))), reverse=True)
    fecha_hoy = NotificarMixin._format_fecha_larga_hoy()
    lineas = [f"\n📋 *Movimientos de hoy — {fecha_hoy}:*"]
    for i, tx in enumerate(txs_ordenadas, 1):
        desc   = NotificarMixin._simplificar_desc_movimiento((tx.get("description") or "—").strip())[:30]
        amount = float(tx.get("amount", 0))
        em     = "🔴" if amount < 0 else "🟢"
        monto  = NotificarMixin._format_monto(amount)
        lineas.append(f"{i}. {em} {desc} — `{monto}`")
    return "\n".join(lineas)


def bridge_conexion_exitosa(cuenta: str, saldo: str, txs: list):
    """
    Notifica a Pochonance que el bot logró entrar a Bancolombia y leer datos
    (saldo + movimientos recientes) por PRIMERA VEZ para esta cuenta desde
    que se activó su monitoreo (ver sesiones.py → MonitorThread._sesion_completa,
    bloque "es_primera_sesion").

    Esta es la prueba real de que la cuenta "sirve": si el bot pudo entrar y
    mandar datos, Pochonance ya puede darla por activa — no hace falta un
    chequeo aparte (por eso ya no existe pochonance.py, que hacía justo eso
    con una sesión de Chrome separada y redundante).

    Parámetros:
        cuenta → nombre de la cuenta
        saldo  → saldo leído al entrar (ej: "$ 1.656.092")
        txs    → lista de movimientos recientes tal como los devuelve la API
                 de Bancolombia — se manda tanto la cantidad como el bloque
                 ya formateado (movimientos_texto), para que el bot "Cuentas
                 P2P" pueda mostrar el mismo primer mensaje que ya se conoce
                 del bot de Movimientos.
    """
    # En simulación Pochonance está cortado, así que el resumen "✅ cuenta —
    # Saldo — Movimientos de hoy" (que normalmente arma el backend y sale por
    # Cuentas P2P) lo mandamos localmente al bot de prueba, para que se vea
    # igual. Cubre los dos puntos desde donde se llama (startup y diferido).
    if _simulacion_activa():
        try:
            from notificar import _safe_send, _leer_chat_ids
            cuerpo = _formatear_movimientos_texto(txs)
            msg = f"✅ *{cuenta}*\n💰 Saldo: `{saldo or '—'}`{cuerpo}"
            for _cid in _leer_chat_ids():
                _safe_send(_cid, msg, parse_mode="Markdown")
            print(f"[{cuenta}] 🧪 Resumen de conexión enviado al bot de prueba")
        except Exception as _e:
            print(f"[{cuenta}] ⚠️ aviso de conexión simulada: {_e}")
        return

    if not _bridge_activo:
        return

    payload = {
        "evento":               "conexion_exitosa",
        "cuenta":                cuenta,
        "saldo_actual":          saldo,
        "cantidad_movimientos":  len(txs or []),
        "movimientos_texto":     _formatear_movimientos_texto(txs),
        "timestamp":             datetime.now().isoformat(),
    }
    _enviar_a_pochonance(payload)


def bridge_error_login(cuenta: str, motivo: str):
    """
    Notifica a Pochonance que el bot INTENTÓ entrar a Bancolombia para esta
    cuenta (recién activada) pero no pudo — con el motivo si se pudo
    determinar (ej: fragmento de un aviso de "cuenta bloqueada/cancelada"
    detectado en la página, ver login.py → _detectar_cuenta_bloqueada) o un
    mensaje genérico si no.

    Solo se dispara mientras la cuenta NUNCA ha logrado conectar ni una vez
    desde que se activó (ver sesiones.py → MonitorThread, guardas sobre
    startup_done) — no se manda por fallos de conexión posteriores en una
    cuenta que ya demostró que funciona.

    Parámetros:
        cuenta → nombre de la cuenta
        motivo → texto explicando por qué no se pudo entrar
    """
    if not _bridge_activo:
        return

    payload = {
        "evento":    "error_login",
        "cuenta":    cuenta,
        "motivo":    motivo or "No se pudo iniciar sesión",
        "timestamp": datetime.now().isoformat(),
    }
    _enviar_a_pochonance(payload)
