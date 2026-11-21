# -*- coding: utf-8 -*-
# =====================================================================
# NOTIFICAR.PY
# Responsabilidad: todo lo relacionado con Telegram y formato de mensajes.
# - Instancia del bot y constantes de configuración
# - Gestión del fichero chats_id.json (agregar, eliminar, listar)
# - Auto-eliminación de chats que bloquearon el bot
# - Funciones de envío (_safe_send, _enviar, _safe_edit, _enviar_a_todos)
# - Utilidades puras compartidas (_parsear_pesos, _es_fatal, pausa_humana)
# - NotificarMixin: métodos de notificación y formateo de transacciones
#
# REGLA DE ORO: OWNER_CHAT_ID (definido en config.py) NUNCA se elimina y SIEMPRE
#               recibe copia de todo mensaje enviado a cualquier chat.
# =====================================================================
import os
import re
import json
import time
import random
import threading
import logging
from datetime import datetime

import telebot

logging.getLogger("TeleBot").setLevel(logging.CRITICAL)

# ── Canal Pochonance (opcional, se desactiva si URL está vacío) ────
# Vive en Movimientos/Pochonance/ junto con pochonance_activador.py (el
# otro script de conexión con la plataforma Pochonance). Se importa acá,
# en un solo lugar, y sesiones.py lo reusa desde notificar.py — así el
# resto del sistema no depende directamente del paquete Pochonance.
try:
    from Pochonance.pochonance_bridge import (
        bridge_movimiento, bridge_cambio_saldo,
        bridge_conexion_exitosa, bridge_error_login,
    )
    _BRIDGE_OK = True
except ImportError:
    _BRIDGE_OK = False
    def bridge_movimiento(*a, **kw): pass
    def bridge_cambio_saldo(*a, **kw): pass
    def bridge_conexion_exitosa(*a, **kw): pass
    def bridge_error_login(*a, **kw): pass

# ── Credenciales desde config.py (no sube a git) ──────────────────
from config import TOKEN_TELEGRAM, OWNER_CHAT_ID
try:
    from config import EXTRA_COMPACT_IDS
except ImportError:
    EXTRA_COMPACT_IDS = set()

bot = telebot.TeleBot(TOKEN_TELEGRAM)

MESES_ES = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"]
MESES_ES_LARGO = ["enero","febrero","marzo","abril","mayo","junio","julio",
                   "agosto","septiembre","octubre","noviembre","diciembre"]

# ══════════════════════════════════════════════════════════════════
# GESTIÓN DE CHAT IDs  (chats_id.json)
# ══════════════════════════════════════════════════════════════════
# Formato del fichero:
#   {"chat_ids": [123456789, 987654321]}
#
CHATS_ID_FILE = os.path.join(os.path.dirname(__file__), "chats_id.json")
_chats_lock   = threading.Lock()

def _leer_chat_ids() -> list[int]:
    """Devuelve la lista de chat_ids. OWNER siempre está incluido."""
    with _chats_lock:
        try:
            with open(CHATS_ID_FILE, encoding="utf-8") as f:
                ids = set(json.load(f).get("chat_ids", []))
        except Exception:
            ids = set()
        ids.add(OWNER_CHAT_ID)
        return list(ids)

def _guardar_chat_ids_raw(ids: set):
    """Escribe el conjunto al JSON. No toca el lock (llamar desde dentro del lock)."""
    with open(CHATS_ID_FILE, "w", encoding="utf-8") as f:
        json.dump({"chat_ids": sorted(ids)}, f)

def guardar_chat_id(chat_id: int):
    """Agrega chat_id al JSON si no estaba. Thread-safe."""
    with _chats_lock:
        try:
            with open(CHATS_ID_FILE, encoding="utf-8") as f:
                ids = set(json.load(f).get("chat_ids", []))
        except Exception:
            ids = set()
        if chat_id not in ids:
            ids.add(chat_id)
            ids.add(OWNER_CHAT_ID)      # OWNER siempre presente
            _guardar_chat_ids_raw(ids)

def _eliminar_chat_id(chat_id: int):
    """
    Elimina chat_id del JSON.
    OWNER (OWNER_CHAT_ID) NUNCA se elimina — esta función es no-op para él.
    """
    if chat_id == OWNER_CHAT_ID:
        return
    with _chats_lock:
        try:
            with open(CHATS_ID_FILE, encoding="utf-8") as f:
                ids = set(json.load(f).get("chat_ids", []))
        except Exception:
            ids = set()
        if chat_id in ids:
            ids.discard(chat_id)
            ids.add(OWNER_CHAT_ID)      # garantía extra
            _guardar_chat_ids_raw(ids)
            print(f"🗑️  Chat ID {chat_id} eliminado del JSON (bot bloqueado/chat no encontrado)")

# ══════════════════════════════════════════════════════════════════
# DETECCIÓN DE ERRORES "BOT BLOQUEADO"
# ══════════════════════════════════════════════════════════════════
_BLOCKED_KW = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot was kicked from the group chat",
    "have no rights to send a message",
    "forbidden",
    "not enough rights",
)

def _es_error_bloqueado(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in _BLOCKED_KW)

# ══════════════════════════════════════════════════════════════════
# ENVÍO SEGURO
# ══════════════════════════════════════════════════════════════════
def _safe_send(chat_id: int, texto: str, parse_mode: str = None, **kwargs) -> bool:
    """
    Intenta enviar un mensaje a chat_id.
    - Si el chat bloqueó el bot o no existe → elimina del JSON (excepto OWNER).
    - Si el error es transitorio (red/DNS/timeout momentáneo), reintenta unas
      pocas veces con espera corta antes de darse por vencido — evita que un
      bache de red de un par de segundos (ej. al arrancar el bot, ver logs de
      "NameResolutionError" en iniciar.py) se traduzca en una notificación
      perdida para siempre, ya que quien llama a esta función marca la
      transacción como "ya notificada" apenas se intenta el envío, sin
      esperar confirmación.
    - Retorna True si el envío fue exitoso.
    """
    # ── MODO SIMULACIÓN: copia al archivo de log ────────────────────
    # No se desvía el envío (así el bot sigue usable con sus botones): el
    # bot de prueba lo da MODO_PRUEBAS. La seguridad de que no llegue a los
    # bots reales la garantiza iniciar.py, que exige MODO_PRUEBAS=True para
    # correr en simulación. Aquí solo se guarda una copia en el archivo.
    try:
        import simulacion as _sim
        if _sim.activo():
            _sim.log_archivo(texto)
    except Exception:
        pass
    _REINTENTOS = 3
    _ESPERA_SEGUNDOS = (2, 5, 10)
    for intento in range(_REINTENTOS):
        try:
            bot.send_message(chat_id, texto, parse_mode=parse_mode, **kwargs)
            return True
        except Exception as e:
            if _es_error_bloqueado(e):
                print(f"⚠️  {chat_id} bloqueó el bot → eliminando del JSON")
                _eliminar_chat_id(chat_id)
                return False
            ultimo_error = e
            # Markdown mal formado (p. ej. un "_" suelto en un nombre o ruta): reintentar YA como texto
            # plano en vez de repetir 3 veces el mismo error y perder el mensaje (28/10: el menú de
            # cuentas no llegó por "cuentas_pochonance.txt").
            if parse_mode is not None and "can't parse entities" in str(e).lower():
                print(f"⚠️  Markdown inválido al enviar a {chat_id} — se reenvía como texto plano")
                parse_mode = None
                continue
            if intento < _REINTENTOS - 1:
                print(f"⚠️  Error enviando a {chat_id} (intento {intento + 1}/{_REINTENTOS}): {e} — reintentando...")
                time.sleep(_ESPERA_SEGUNDOS[intento])
    print(f"⚠️  Error enviando a {chat_id} tras {_REINTENTOS} intentos: {ultimo_error}")
    return False

def _enviar(chat_id: int, texto: str, parse_mode: str = None):
    """
    Envía a chat_id.
    OWNER siempre recibe copia si chat_id != OWNER.
    Si chat_id bloqueó el bot → se elimina del JSON.
    """
    _safe_send(chat_id, texto, parse_mode=parse_mode)
    if chat_id != OWNER_CHAT_ID:
        _safe_send(OWNER_CHAT_ID, texto, parse_mode=parse_mode)

def _enviar_a_todos(texto: str, parse_mode: str = None, excluir: set = None):
    """
    Broadcast a TODOS los chat_ids del JSON.
    Los que bloqueen el bot se eliminan automáticamente.
    OWNER siempre recibe, incluso si está en excluir.
    """
    excluir = excluir or set()
    for cid in _leer_chat_ids():
        if cid == OWNER_CHAT_ID or cid not in excluir:
            _safe_send(cid, texto, parse_mode=parse_mode)

def _safe_edit(chat_id, msg_id, text, reply_markup=None, parse_mode=None):
    try:
        bot.edit_message_text(text, chat_id, msg_id,
                              reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        if "message is not modified" not in str(e):
            print(f"⚠️  edit_message_text: {e}")

# ══════════════════════════════════════════════════════════════════
# UTILIDADES PURAS
# ══════════════════════════════════════════════════════════════════
def pausa_humana(min_ms: int = 200, max_ms: int = 600):
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))

def _parsear_pesos(texto: str) -> float | None:
    try:
        clean = re.sub(r'[^\d,.\-]', '', (texto or '').strip())
        if not clean:
            return None
        negativo = clean.startswith('-')
        clean = clean.lstrip('-')
        if ',' in clean:
            clean = clean.replace('.', '').replace(',', '.')
        elif '.' in clean:
            partes = clean.split('.')
            if len(partes) > 2:
                clean = clean.replace('.', '')
            elif len(partes[1]) == 3:
                clean = clean.replace('.', '')
        valor = float(clean)
        return -valor if negativo else valor
    except Exception:
        return None

_CRASH_KW = (
    "target closed", "execution context", "page crashed",
    "browsercontext", "connection closed", "ws connect",
    "browser has disconnected", "protocol error",
    "out of memory",
    "no se encuentra el sidebar",
    "sesión cerrada por bancolombia",
)

def _es_fatal(e: Exception) -> bool:
    return any(k in str(e).lower() for k in _CRASH_KW)

# ══════════════════════════════════════════════════════════════════
# NotificarMixin
# Métodos de formato y notificación de transacciones Bancolombia.
# Se mezcla en MonitorThread vía herencia múltiple (sesiones.py).
# ══════════════════════════════════════════════════════════════════
class NotificarMixin:

    # ── Formato ───────────────────────────────────────────────────
    @staticmethod
    def _format_fecha(date_str: str) -> str:
        """'2026/08/03' → '03 ago 2026'"""
        from datetime import datetime
        try:
            dt = datetime.strptime(date_str, "%Y/%m/%d")
            return f"{dt.day:02d} {MESES_ES[dt.month-1]} {dt.year}"
        except Exception:
            return date_str or "—"

    @staticmethod
    def _format_monto(amount: float) -> str:
        """52000.0 → '$ 52.000'  |  5.68 → '$ 5,68'"""
        abs_amt  = abs(amount)
        int_part = int(abs_amt)
        dec_part = round((abs_amt - int_part) * 100)
        int_str  = f"{int_part:,}".replace(",", ".")
        return f"$ {int_str},{dec_part:02d}" if dec_part else f"$ {int_str}"

    @staticmethod
    def _format_fecha_larga_hoy() -> str:
        """'18 de septiembre' — fecha de HOY en español, sin año y sin cero
        a la izquierda (18/09/2026, a pedido del equipo: en 'Movimientos de
        hoy' cada línea repetía la fecha del día, siendo que ya se sabe que
        es hoy — ahora la fecha va UNA sola vez en el encabezado)."""
        hoy = datetime.now()
        return f"{hoy.day} de {MESES_ES_LARGO[hoy.month-1]}"

    @staticmethod
    def _simplificar_desc_movimiento(desc: str) -> str:
        """Acorta descripciones largas y repetitivas del banco en la lista de
        'Movimientos de hoy' (18/09/2026, a pedido del equipo). Por ahora solo
        el caso "TRANSFERENCIA ... SUC VIRTUAL" → "TRANS SUC VIRTUAL" — el
        resto (ej. "RETIRO CORRESPONSAL") se deja tal cual, a propósito."""
        d = (desc or "").upper()
        if "TRANSFERENCIA" in d and "SUC VIRTUAL" in d:
            return "TRANS SUC VIRTUAL"
        if "NEQUI" in d:
            return "NEQUI"
        return desc

    @staticmethod
    def _desc_corta(desc: str) -> str:
        """Extrae una etiqueta breve de la descripción Bancolombia."""
        d = desc.upper()
        if "NEQUI"            in d: return "NEQUI"
        if "SUC VIRTUAL"      in d: return "SUC VIRTUAL"
        if "SUCURSAL VIRTUAL" in d: return "SUC VIRTUAL"
        if "DAVIPLATA"        in d: return "DAVIPLATA"
        if "CORRESPONSAL"     in d: return "CORRESPONSAL"
        if "CAJERO"           in d: return "CAJERO ATM"
        if "ATM"              in d: return "CAJERO ATM"
        if "EFECTY"           in d: return "EFECTY"
        if "PSE"              in d: return "PSE"
        if "4X1000"           in d: return "4×1000"
        if "IMPTO"            in d: return "IMPUESTO"
        if "IMPUESTO"         in d: return "IMPUESTO"
        if "COMISION"         in d: return "COMISIÓN"
        if "CONSIGNACION"     in d: return "CONSIGNACIÓN"
        if "RETIRO"           in d: return "RETIRO"
        if "TRANSFERENCIA"    in d: return "TRANSFERENCIA"
        palabras = desc.split()
        return " ".join(palabras[:2]) if len(palabras) >= 2 else desc[:20]

    # ── Notificación individual ────────────────────────────────────
    def _notificar_tx(self, tx: dict, saldo_anterior: str, saldo_actual: str):
        desc     = (tx.get("description") or "—").strip()
        amount   = float(tx.get("amount", 0))
        fecha    = self._format_fecha(tx.get("transactionDate", ""))
        ref1     = (tx.get("reference1") or "").strip()

        monto     = self._format_monto(amount)
        es_salida = amount < 0
        emoji     = "🔴" if es_salida else "🟢"
        accion    = "Salió" if es_salida else "Entró"

        # ── Mensaje completo (owner siempre lo recibe) ─────────────
        lineas_full = [
            f"{emoji} *{self.nombre}*",
            f"💰 Saldo actual:   `{saldo_actual or '—'}`",
            f"📊 Saldo anterior: `{saldo_anterior or '—'}`",
            f"{'📤' if es_salida else '📥'} {accion}: `{monto}`",
            f"📝 {desc}",
            f"📅 {fecha}",
        ]
        if ref1:
            lineas_full.append(f"🔖 {ref1}")
        msg_full = "\n".join(lineas_full)

        # ── Mensaje compacto (operadores externos) ─────────────────
        desc_breve = self._desc_corta(desc)
        msg_corto = (
            f"{emoji} *{self.nombre}*\n"
            f"{'📤' if es_salida else '📥'} {accion}: `{monto}`\n"
            f"💳 {desc_breve}"
        )

        # ── Despacho diferenciado ──────────────────────────────────
        # Solo OWNER_CHAT_ID recibe el mensaje completo.
        # Todos los demás (operadores, clientes) reciben el compacto.
        if self.chat_id == OWNER_CHAT_ID:
            _safe_send(OWNER_CHAT_ID, msg_full, parse_mode="Markdown")
        else:
            _safe_send(self.chat_id, msg_corto, parse_mode="Markdown")
            _safe_send(OWNER_CHAT_ID, msg_full, parse_mode="Markdown")

        # ── Destinatarios extra compactos (redundancia Pochonance) ───
        # Reciben msg_corto siempre, directo por Bot Saldo, sin pasar
        # por Pochonance — garantía de llegada aunque Railway esté caído.
        for _eid in EXTRA_COMPACT_IDS:
            if _eid not in (self.chat_id, OWNER_CHAT_ID):
                _safe_send(_eid, msg_corto, parse_mode="Markdown")

        # Medición: la hora de esta línea es cuando salió por Bot Saldo.
        print(f"[{self.nombre}] 📨 Bot Saldo notificado: {accion} {monto}")

        # ── Canal Pochonance (segundo canal, no bloquea Telegram) ──
        bridge_movimiento(
            cuenta=self.nombre,
            tx=tx,
            saldo_anterior=saldo_anterior,
            saldo_actual=saldo_actual,
            desc_corta=self._desc_corta(desc),
        )

    # ── Notificación de lote (calcula saldos intermedios) ─────────
    def _notificar_tx_batch(self, nuevas: list, saldo_base: str) -> str:
        """
        Notifica un lote de txs nuevas calculando los saldos intermedios.
        Retorna el saldo final como string.
        """
        base_val = _parsear_pesos(saldo_base) or 0.0
        sc = base_val
        batch: list = []
        for tx in reversed(nuevas):
            m    = float(tx.get("amount", 0))
            sa   = "$ " + f"{int(round(sc)):,}".replace(",", ".")
            sc  += m
            sact = "$ " + f"{int(round(sc)):,}".replace(",", ".")
            batch.append((tx, sa, sact))
        def _send(_b=batch):
            for _t, _sa, _sact in _b:
                self._notificar_tx(_t, _sa, _sact)
        threading.Thread(target=_send, daemon=True).start()
        return "$ " + f"{int(round(sc)):,}".replace(",", ".")

    # ── Notificación DOM (fallback cuando XHR no capturó la tx) ───
    def _notificar_dom_alerta(self, fila_texto: str, saldo_anterior: str, saldo_actual: str):
        """Movimiento detectado por la tabla (respaldo). Mismo formato y mismo
        reparto completo/compacto que _notificar_tx; solo agrega una línea que
        indica que vino de la tabla."""
        from filtro_dom import partes_fila   # import tardío: filtro_dom importa notificar
        p = partes_fila(fila_texto)
        if p["monto"] is None:
            # Fila sin monto legible: aviso simple (sin escapes visibles)
            msg = (f"⚠️ *{self.nombre}* — movimiento detectado en la tabla\n"
                   f"💰 `{saldo_anterior}` → `{saldo_actual}`\n"
                   f"📋 {fila_texto[:150].replace('`', '').replace('_', ' ').replace('*', '')}")
            _enviar(self.chat_id, msg, parse_mode="Markdown")
            return

        amount    = p["monto"]
        es_salida = amount < 0
        emoji     = "🔴" if es_salida else "🟢"
        accion    = "Salió" if es_salida else "Entró"
        monto     = self._format_monto(amount)
        desc      = p["desc"].replace("`", "").replace("_", " ").replace("*", "")
        icono     = "📤" if es_salida else "📥"

        lineas_full = [
            f"{emoji} *{self.nombre}*",
            f"💰 Saldo actual:   `{saldo_actual or '—'}`",
            f"📊 Saldo anterior: `{saldo_anterior or '—'}`",
            f"{icono} {accion}: `{monto}`",
            f"📝 {desc}",
        ]
        if p["fecha"]:
            lineas_full.append(f"📅 {p['fecha']}")
        if p["ref"]:
            lineas_full.append(f"🔖 {p['ref'].replace('`', '')}")
        lineas_full.append("🗂️ _detectado en la tabla (respaldo)_")
        msg_full  = "\n".join(lineas_full)
        msg_corto = (f"{emoji} *{self.nombre}*\n"
                     f"{icono} {accion}: `{monto}`\n"
                     f"💳 {self._desc_corta(desc)}")

        if self.chat_id == OWNER_CHAT_ID:
            _safe_send(OWNER_CHAT_ID, msg_full, parse_mode="Markdown")
        else:
            _safe_send(self.chat_id, msg_corto, parse_mode="Markdown")
            _safe_send(OWNER_CHAT_ID, msg_full, parse_mode="Markdown")
        for _eid in EXTRA_COMPACT_IDS:
            if _eid not in (self.chat_id, OWNER_CHAT_ID):
                _safe_send(_eid, msg_corto, parse_mode="Markdown")
        print(f"[{self.nombre}] 📨 Bot Saldo notificado (tabla): {accion} {monto}")

    # ── Notificación por delta de saldo (último recurso) ──────────
    def _notificar_saldo_cambio_inferido(
        self, txs: list, saldo_anterior: str, saldo_actual: str, delta: float
    ):
        if not txs or abs(delta) < 0.01:
            return

        # Caso 1: una sola tx coincide con el delta
        for tx in txs:
            if abs(float(tx.get("amount", 0)) - delta) < 1.0:
                self._notificar_tx(tx, saldo_anterior, saldo_actual)
                return

        # Caso 2: prefijo de N txs suma al delta
        acum = 0.0
        txs_combo: list = []
        for tx in txs:
            acum += float(tx.get("amount", 0))
            txs_combo.append(tx)
            if abs(acum - delta) < 1.0:
                break
        if abs(acum - delta) >= 1.0:
            txs_combo = []

        if txs_combo:
            val_ant = _parsear_pesos(saldo_anterior) or 0.0
            saldo_corriente = val_ant
            for tx in reversed(txs_combo):
                monto_tx = float(tx.get("amount", 0))
                s_ant_str = "$ " + f"{int(round(saldo_corriente)):,}".replace(",", ".")
                saldo_corriente += monto_tx
                s_act_str = "$ " + f"{int(round(saldo_corriente)):,}".replace(",", ".")
                self._notificar_tx(tx, s_ant_str, s_act_str)
            return

        # Caso 3: sin coincidencia → solo cambio de saldo
        _amts = [float(t.get("amount", 0)) for t in txs[:6]]
        print(f"[{self.nombre}] 🔍 Case3 diagnóstico: delta={delta:+.2f}, "
              f"primeros montos en txs: {_amts}")
        monto  = self._format_monto(abs(delta)) if abs(delta) >= 0.01 else "—"
        emoji  = "🔴" if delta < 0 else "🟢"
        lineas = [
            f"{emoji} *{self.nombre}* — cambio de saldo",
            f"💰 `{saldo_anterior}` → `{saldo_actual}`",
            f"{'📤' if delta < 0 else '📥'} Delta: `{monto}`",
        ]
        _enviar(self.chat_id, "\n".join(lineas), parse_mode="Markdown")

        # ── Destinatarios extra compactos ─────────────────────────
        for _eid in EXTRA_COMPACT_IDS:
            if _eid not in (self.chat_id, OWNER_CHAT_ID):
                _safe_send(_eid, "\n".join(lineas), parse_mode="Markdown")

        # ── Canal Pochonance ──────────────────────────────────────
        bridge_cambio_saldo(
            cuenta=self.nombre,
            saldo_anterior=saldo_anterior,
            saldo_actual=saldo_actual,
            delta=delta,
        )

    # ── Movimientos del día (snapshot bajo demanda) ────────────────
    def _notificar_movimientos_hoy(self, txs_hoy: list, chat_id: int | None):
        """
        Envía al chat_id la lista de txs de hoy formateada como lista numerada.
        _enviar() ya manda copia automática a OWNER_CHAT_ID — no hay que hacerlo
        explícitamente (evita doble mensaje al owner).
        Si chat_id es None, envía solo al owner.
        """
        destino  = chat_id if chat_id is not None else OWNER_CHAT_ID
        hoy_str  = datetime.now().strftime("%d/%m/%Y")

        if not txs_hoy:
            msg = (
                f"📋 *{self.nombre}* — Movimientos del día\n"
                f"📅 {hoy_str}\n\n"
                f"_Sin movimientos hoy_"
            )
            _enviar(destino, msg, parse_mode="Markdown")
            return

        lineas = [
            f"📋 *{self.nombre}* — Movimientos del día",
            f"📅 {hoy_str} \\| {len(txs_hoy)} movimiento(s)\n",
        ]
        for i, tx in enumerate(txs_hoy, 1):
            monto  = float(tx.get("amount", 0))
            emoji  = "🟢" if monto >= 0 else "🔴"
            desc   = self._simplificar_desc_movimiento((tx.get("description") or "—"))[:28].replace("*","").replace("_","")
            ref    = (tx.get("reference1") or tx.get("reference2") or "—")[:20]
            monto_fmt = "$ " + f"{abs(int(round(monto))):,}".replace(",",".")
            signo  = "+" if monto >= 0 else "-"
            lineas.append(
                f"{i}\\. {emoji} `{desc:<28}` "
                f"*{signo}{monto_fmt}*\n"
                f"   📌 Ref: `{ref}`"
            )

        _enviar(destino, "\n".join(lineas), parse_mode="Markdown")
