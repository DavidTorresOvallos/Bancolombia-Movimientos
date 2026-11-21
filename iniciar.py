# -*- coding: utf-8 -*-
# =====================================================================
# INICIAR.PY
# Responsabilidad: punto de entrada del sistema. Activar para encender todo.
# - Limpieza de caché al arrancar
# - Handlers de Telegram (menú, selección de cuentas, callbacks)
# - Polling loop del bot
#
# USO:
#   cd "C:\Users\Usuario\Documents\AutomatizacionBancolombia\Movimientos"
#   python iniciar.py
# =====================================================================
import sys
# Configurar UTF-8 sin reemplazar el stream completo (el reemplazo bloquea
# el flush en hilos secundarios en Windows y suprime todo el output del monitoreo).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Hora exacta [HH:MM:SS] al inicio de cada línea de la terminal, para medir
# cuánto tarda cada paso (ciclos, reinicios, notificaciones). Se reemplaza
# print() y NO sys.stdout (ver nota de arriba: reemplazar el stream rompe
# el output de los hilos en Windows). Va antes de los demás imports para
# que también cubra lo que imprimen los módulos al cargarse.
import builtins
import time as _time_log

if not getattr(builtins.print, "_con_hora", False):   # una sola vez, aunque se recargue
    _print_original = builtins.print

    # consola.py: print() deja la línea en una cola y vuelve al instante; dos hilos
    # aparte escriben al archivo logs/ y a la pantalla. Así un clic o una selección
    # de texto en la ventana ya no congela a todas las cuentas (ver consola.py).
    # NO reemplaza sys.stdout (ver nota de arriba).
    try:
        import consola as _consola
        _consola.instalar(_print_original)
    except Exception:
        _consola = None

    def _print_con_hora(*args, sep=" ", end="\n", file=None, flush=False):
        if file is not None and file is not sys.stdout:
            return _print_original(*args, sep=sep, end=end, file=file, flush=flush)
        texto = sep.join(str(a) for a in args)
        if texto.strip():
            hora  = _time_log.strftime("%H:%M:%S")
            texto = "\n".join(
                f"[{hora}] {linea}" if linea.strip() else linea
                for linea in texto.split("\n")
            )
        if _consola is not None and _consola.activo():
            _consola.emitir(texto, end)   # nunca bloquea al hilo que imprime
            return None
        return _print_original(texto, end=end, flush=flush)

    _print_con_hora._con_hora = True
    builtins.print = _print_con_hora

import os
import re
import time
import threading
import shutil
import subprocess

import telebot

from notificar import (
    bot, _enviar, _safe_edit, _safe_send, OWNER_CHAT_ID,
    guardar_chat_id, _leer_chat_ids, _enviar_a_todos,
)
from login import listar_cuentas_bw, obtener_credenciales, _teclado_seleccion
from sesiones import MonitorThread, actualizar_conteo_sesiones
from movimiento import CACHE_DIR
# USER_DATA_DIR para limpiar caché de Chrome
from sesiones import USER_DATA_DIR
# Activación de cuentas disparada desde Pochonance (sin Telegram) — toda
# la lógica vive en Pochonance/pochonance_activador.py, ver el contrato
# documentado ahí. Carpeta Pochonance/ agrupa todos los scripts que
# conectan este sistema con la plataforma Pochonance.
from Pochonance.pochonance_activador import iniciar_polling_activacion, NODO_ID as _NODO_ID
import nodos as _nodos   # qué cuentas atiende este PC (por ID de Pochonance)

# Nodo secundario (ej. "B"): usa el MISMO bot de Telegram que el A, y Telegram
# solo permite UN getUpdates por token, así que no escucha ni manda el menú
# (eso es del A). Sí manda sus notificaciones (sendMessage no tiene esa restricción).
_SIN_MENU_TELEGRAM = _NODO_ID != "A"

# ── Estado global ──────────────────────────────────────────────────
_sesiones_activas: dict  = {}   # {nombre: MonitorThread}
_estado_seleccion: dict  = {}   # {chat_id: {cuentas, seleccionadas, ya_activas}}

def _borrar_cache_cuenta(nombre: str):
    """Borra el archivo de caché de una cuenta. Al reiniciar queda como sesión nueva."""
    safe  = re.sub(r"[^\w]", "_", nombre)
    ruta  = os.path.join(CACHE_DIR, f"cache2_{safe}.json")
    if os.path.exists(ruta):
        try:
            os.remove(ruta)
            print(f"🗑️  Caché de {nombre} eliminada")
        except Exception as e:
            print(f"⚠️  No se pudo borrar caché de {nombre}: {e}")

def _actualizar_conteo():
    """Notifica a sesiones.py cuántas sesiones están vivas para ajustar el límite de RAM."""
    n = sum(1 for m in _sesiones_activas.values() if m.is_alive())
    actualizar_conteo_sesiones(max(1, n))

def _borrar_cache_todas():
    """Borra todos los archivos cache2_*.json de movimientos_cache."""
    try:
        borrados = 0
        for f in os.listdir(CACHE_DIR):
            if f.startswith("cache2_") and f.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, f)); borrados += 1
                except Exception:
                    pass
        if borrados:
            print(f"🗑️  {borrados} caché(s) eliminada(s)")
    except Exception:
        pass

# ── Menú principal ─────────────────────────────────────────────────
def _teclado_menu_principal():
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(telebot.types.InlineKeyboardButton(
        "🚀  Iniciar monitoreo", callback_data="menu:iniciar"))
    markup.row(
        telebot.types.InlineKeyboardButton("⏸  Pausar cuenta",   callback_data="menu:pausar_lista"),
        telebot.types.InlineKeyboardButton("▶️  Reanudar cuenta", callback_data="menu:reanudar_lista"),
    )
    markup.row(
        telebot.types.InlineKeyboardButton("🛑  Detener cuenta",  callback_data="menu:detener_cuenta"),
        telebot.types.InlineKeyboardButton("🛑  Detener todo",    callback_data="menu:detener"),
    )
    markup.add(telebot.types.InlineKeyboardButton("📊  Estado",         callback_data="menu:estado"))
    markup.add(telebot.types.InlineKeyboardButton("📋  Movimientos hoy", callback_data="menu:movimientos_hoy"))
    markup.row(
        telebot.types.InlineKeyboardButton("🔄  Reiniciar bot",    callback_data="menu:reiniciar"),
        telebot.types.InlineKeyboardButton("🔃  Actualizar cuentas", callback_data="menu:actualizar_bw"),
    )
    return markup

def enviar_menu(chat_id: int, texto: str = "¿Qué deseas hacer?"):
    _safe_send(chat_id, texto, reply_markup=_teclado_menu_principal(), parse_mode="Markdown")

# Chat ID persistence → delegated to notificar.py (guardar_chat_id / _leer_chat_ids)

def _reiniciar_proceso():
    for _m in list(_sesiones_activas.values()):
        try: _m.stop()
        except Exception: pass
    _sesiones_activas.clear()
    time.sleep(2)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen([sys.executable] + sys.argv, **kwargs)
    os._exit(0)

# ══════════════════════════════════════════════════════════════════
# HANDLERS DE TELEGRAM
# ══════════════════════════════════════════════════════════════════
@bot.message_handler(commands=['start', 'ayuda', 'help', 'menu'])
def handler_menu(message):
    guardar_chat_id(message.chat.id)
    enviar_menu(message.chat.id, "👋 *Bot Bancolombia 2.0*\n\nSelecciona una opción:")

@bot.callback_query_handler(func=lambda c: c.data.startswith("menu:"))
def handler_menu_callback(call):
    chat_id = call.message.chat.id
    guardar_chat_id(chat_id)
    accion = call.data[len("menu:"):]

    def _ack(txt=""):
        try: bot.answer_callback_query(call.id, txt)
        except Exception: pass

    if accion == "iniciar":
        _ack("Cargando cuentas...")
        _safe_edit(chat_id, call.message.message_id, "🔄 Cargando cuentas de Bitwarden...")
        def _cargar():
            cuentas, error = listar_cuentas_bw()
            if error or not cuentas:
                msg = f"❌ *No se encontraron cuentas.*\n\n`{error}`" if error else "❌ Sin cuentas."
                _safe_send(chat_id, msg, parse_mode="Markdown")
                enviar_menu(chat_id); return
            # Solo las cuentas de ESTE nodo (por ID de Pochonance, ver nodos.py).
            cuentas, _ocultas = _nodos.filtrar_manual(cuentas)
            if not cuentas:
                _safe_send(chat_id, f"⛔ Ninguna cuenta de Bitwarden pertenece al nodo {_NODO_ID}. "
                                    f"{_nodos.ERROR_RANGOS or _nodos.AYUDA_MANUAL}")
                enviar_menu(chat_id); return
            ya_activas = {n for n, m in _sesiones_activas.items() if m.is_alive()}
            _estado_seleccion[chat_id] = {
                "cuentas":      cuentas,
                "seleccionadas": set(),
                "ya_activas":   ya_activas,
            }
            lineas = [f"📋 *{len(cuentas)} cuenta(s) disponibles.*"]
            if _ocultas:
                lineas.append(f"🔒 {_ocultas} oculta(s): son de otro nodo o no tienen ID en `cuentas_pochonance.txt`.")
            if ya_activas:
                lineas.append(
                    f"🟢 *{len(ya_activas)} ya activa(s)* — siguen corriendo sin interrupción."
                )
            lineas.append("Selecciona las que quieres *agregar* al monitoreo:")
            _safe_send(
                chat_id,
                "\n".join(lineas),
                reply_markup=_teclado_seleccion(cuentas, set(), ya_activas),
                parse_mode="Markdown"
            )
        threading.Thread(target=_cargar, daemon=True).start()
        return

    if accion == "pausar_lista":
        activas = {n: m for n, m in _sesiones_activas.items()
                   if m.is_alive() and not m.esta_pausado}
        if not activas:
            _ack("No hay cuentas activas sin pausar."); return
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for nombre in sorted(activas):
            markup.add(telebot.types.InlineKeyboardButton(
                f"⏸  {nombre}", callback_data=f"cuenta_pausar:{nombre[:50]}"
            ))
        markup.add(telebot.types.InlineKeyboardButton("↩️  Volver", callback_data="menu:volver"))
        _ack()
        _safe_edit(chat_id, call.message.message_id, "⏸ *¿Qué cuenta quieres pausar?*",
                   reply_markup=markup, parse_mode="Markdown")
        return

    if accion == "reanudar_lista":
        pausadas = {n: m for n, m in _sesiones_activas.items() if m.esta_pausado}
        if not pausadas:
            _ack("No hay cuentas pausadas."); return
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for nombre in sorted(pausadas):
            markup.add(telebot.types.InlineKeyboardButton(
                f"▶️  {nombre}", callback_data=f"cuenta_reanudar:{nombre[:50]}"
            ))
        markup.add(telebot.types.InlineKeyboardButton("↩️  Volver", callback_data="menu:volver"))
        _ack()
        _safe_edit(chat_id, call.message.message_id, "▶️ *¿Qué cuenta quieres reanudar?*",
                   reply_markup=markup, parse_mode="Markdown")
        return

    if accion == "detener_cuenta":
        activas = {n: m for n, m in _sesiones_activas.items() if m.is_alive()}
        if not activas:
            _ack("No hay cuentas activas."); enviar_menu(chat_id); return
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for nombre in sorted(activas):
            markup.add(telebot.types.InlineKeyboardButton(
                f"🛑  {nombre}", callback_data=f"cuenta_detener:{nombre[:50]}"
            ))
        markup.add(telebot.types.InlineKeyboardButton("↩️  Volver", callback_data="menu:volver"))
        _ack()
        _safe_edit(chat_id, call.message.message_id, "🛑 *¿Qué cuenta quieres detener?*",
                   reply_markup=markup, parse_mode="Markdown")
        return

    if accion == "detener":
        if not _sesiones_activas:
            _ack("No hay sesiones activas."); enviar_menu(chat_id); return
        nombres = sorted(_sesiones_activas.keys())
        sesiones_copia = dict(_sesiones_activas)
        _sesiones_activas.clear()
        _actualizar_conteo()
        _borrar_cache_todas()
        _ack("Detenido.")
        _safe_edit(chat_id, call.message.message_id, f"🛑 Detenido: {', '.join(nombres)}")
        enviar_menu(chat_id)
        # stop() en hilo separado: no bloquear el handler de Telegram.
        # stop() solo señaliza (no llama Playwright cross-thread) → seguro en daemon.
        def _parar_todo(ses=sesiones_copia):
            for m in ses.values():
                try: m.stop()
                except Exception: pass
        threading.Thread(target=_parar_todo, daemon=True).start()
        return

    if accion == "estado":
        _ack()
        if not _sesiones_activas:
            enviar_menu(chat_id, "ℹ️ Sin sesiones activas."); return
        lineas = ["📊 *Sesiones activas (2.0):*"]
        for nombre, m in sorted(_sesiones_activas.items()):
            vivo   = "✅" if m.is_alive() else "❌"
            estado = "⏸ Pausado" if m.esta_pausado else "▶️ Activo"
            saldo  = m.saldo_actual or "—"
            n_txs  = len(m.txs_conocidas)
            lineas.append(
                f"{vivo} *{nombre}* — {estado}\n   💰 `{saldo}` | {n_txs} txs conocidas"
            )
        _safe_send(chat_id, "\n".join(lineas), parse_mode="Markdown")
        enviar_menu(chat_id); return

    if accion == "actualizar_bw":
        _ack("Actualizando cuentas...")
        _safe_edit(chat_id, call.message.message_id, "🔃 Actualizando lista desde Bitwarden...")
        def _actualizar_bw():
            import login as _login_mod
            _login_mod._bw_items_cache = []   # invalidar caché → próxima llamada va a BW
            cuentas, error = listar_cuentas_bw(forzar=True)
            if error or not cuentas:
                _safe_send(chat_id, f"❌ Error actualizando BW: `{error}`", parse_mode="Markdown")
            else:
                _safe_send(chat_id, f"✅ Cuentas actualizadas: {len(cuentas)} en Bitwarden.")
            enviar_menu(chat_id)
        threading.Thread(target=_actualizar_bw, daemon=True).start()
        return

    if accion == "reiniciar":
        _ack("Reiniciando...")
        _safe_edit(chat_id, call.message.message_id,
                   "🔄 *Reiniciando bot...*\nVuelve en unos segundos y escribe /menu.",
                   parse_mode="Markdown")
        threading.Thread(target=_reiniciar_proceso, daemon=True).start()
        return

    if accion == "movimientos_hoy":
        _ack()
        activas = list(_sesiones_activas.items())
        if not activas:
            _safe_edit(chat_id, call.message.message_id,
                       "ℹ️ Sin sesiones activas.",
                       reply_markup=telebot.types.InlineKeyboardMarkup().add(
                           telebot.types.InlineKeyboardButton("↩️ Menú", callback_data="menu:volver")
                       ))
            return
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for nombre, _ in sorted(activas):
            markup.add(telebot.types.InlineKeyboardButton(
                nombre, callback_data=f"cuenta_movhoy:{nombre[:60]}"
            ))
        markup.add(telebot.types.InlineKeyboardButton("↩️ Cancelar", callback_data="menu:volver"))
        _safe_edit(chat_id, call.message.message_id,
                   "📋 *Movimientos del día* — ¿De cuál cuenta?",
                   reply_markup=markup, parse_mode="Markdown")
        return

    if accion == "volver":
        _ack()
        _safe_edit(chat_id, call.message.message_id, "¿Qué deseas hacer?",
                   reply_markup=_teclado_menu_principal()); return

@bot.callback_query_handler(func=lambda c: c.data.startswith("cuenta_movhoy:"))
def handler_movhoy_callback(call):
    chat_id = call.message.chat.id
    def _ack(txt=""):
        try: bot.answer_callback_query(call.id, txt)
        except Exception: pass

    nombre  = call.data[len("cuenta_movhoy:"):]
    monitor = _sesiones_activas.get(nombre)
    if not monitor:
        _ack("No encontrado.")
        _safe_edit(chat_id, call.message.message_id,
                   "❌ Cuenta no encontrada o ya detenida.",
                   reply_markup=telebot.types.InlineKeyboardMarkup().add(
                       telebot.types.InlineKeyboardButton("↩️ Menú", callback_data="menu:volver")
                   ))
        return

    monitor._snapshot_chat_id = chat_id
    monitor._snapshot_solicitado.set()
    _ack("📋 Consultando movimientos...")
    _safe_edit(chat_id, call.message.message_id,
               f"⏳ Consultando movimientos de hoy de *{nombre}*\\.\n"
               f"_El mensaje llega en el próximo ciclo del monitor\\._",
               reply_markup=telebot.types.InlineKeyboardMarkup().add(
                   telebot.types.InlineKeyboardButton("↩️ Menú", callback_data="menu:volver")
               ), parse_mode="Markdown")


@bot.callback_query_handler(func=lambda c: (
    c.data.startswith("cuenta_pausar:") or
    c.data.startswith("cuenta_reanudar:") or
    c.data.startswith("cuenta_detener:")
))
def handler_cuenta_callback(call):
    chat_id = call.message.chat.id
    def _ack(txt=""):
        try: bot.answer_callback_query(call.id, txt)
        except Exception: pass

    if call.data.startswith("cuenta_pausar:"):
        nombre  = call.data[len("cuenta_pausar:"):]
        monitor = _sesiones_activas.get(nombre)
        if not monitor: _ack("No encontrado."); return
        monitor.pausar(minutos=10)
        _ack(f"⏸ {nombre} pausada")
        _safe_edit(chat_id, call.message.message_id,
                   f"⏸ *{nombre}* pausada 10 min.\nUsa *▶️ Reanudar* cuando termines.",
                   reply_markup=telebot.types.InlineKeyboardMarkup().add(
                       telebot.types.InlineKeyboardButton(
                           f"▶️ Reanudar {nombre}",
                           callback_data=f"cuenta_reanudar:{nombre[:50]}"
                       ),
                       telebot.types.InlineKeyboardButton("↩️ Menú", callback_data="menu:volver")
                   ), parse_mode="Markdown")

    elif call.data.startswith("cuenta_detener:"):
        nombre  = call.data[len("cuenta_detener:"):]
        monitor = _sesiones_activas.get(nombre)
        if not monitor: _ack("No encontrado."); return
        _sesiones_activas.pop(nombre, None)
        _actualizar_conteo()
        _borrar_cache_cuenta(nombre)
        _ack(f"🛑 {nombre} detenida")
        _safe_edit(chat_id, call.message.message_id,
                   f"🛑 *{nombre}* detenida. Las demás siguen activas.",
                   reply_markup=telebot.types.InlineKeyboardMarkup().add(
                       telebot.types.InlineKeyboardButton("↩️ Menú", callback_data="menu:volver")
                   ), parse_mode="Markdown")
        # stop() en hilo separado para no bloquear el handler de Telegram
        def _parar(m=monitor):
            try: m.stop()
            except Exception: pass
        threading.Thread(target=_parar, daemon=True).start()

    elif call.data.startswith("cuenta_reanudar:"):
        nombre  = call.data[len("cuenta_reanudar:"):]
        monitor = _sesiones_activas.get(nombre)
        if not monitor: _ack("No encontrado."); return
        monitor.reanudar()
        _ack(f"▶️ {nombre} reanudada")
        _safe_edit(chat_id, call.message.message_id,
                   f"▶️ *{nombre}* reanudada. Reconectando...",
                   reply_markup=telebot.types.InlineKeyboardMarkup().add(
                       telebot.types.InlineKeyboardButton("↩️ Menú", callback_data="menu:volver")
                   ), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("mon_"))
def handler_monitor_callback(call):
    chat_id = call.message.chat.id
    def _ack(txt=""):
        try: bot.answer_callback_query(call.id, txt)
        except Exception: pass
    estado = _estado_seleccion.get(chat_id)

    if call.data == "mon_cancelar":
        _ack("Cancelado.")
        _safe_edit(chat_id, call.message.message_id, "❌ Cancelado.")
        _estado_seleccion.pop(chat_id, None)
        enviar_menu(chat_id); return

    if call.data == "mon_comenzar":
        if not estado or not estado["seleccionadas"]:
            _ack("Selecciona al menos una cuenta."); return
        seleccionadas = set(estado["seleccionadas"])
        _ack(f"Iniciando {len(seleccionadas)} sesión(es)...")
        _safe_edit(
            chat_id, call.message.message_id,
            f"🚀 Iniciando monitoreo de: *{', '.join(sorted(seleccionadas))}*",
            parse_mode="Markdown"
        )
        _estado_seleccion.pop(chat_id, None)

        def _arrancar():
            cuentas_a_iniciar = []
            for nombre in sorted(seleccionadas):
                if nombre in _sesiones_activas and _sesiones_activas[nombre].is_alive():
                    _safe_send(chat_id,
                        f"ℹ️ *{nombre}* ya está activa — se deja corriendo sin cambios.",
                        parse_mode="Markdown")
                    continue
                _ok_nodo, _motivo_nodo = _nodos.decidir_manual(nombre)
                if not _ok_nodo:
                    _safe_send(chat_id, f"⛔ {nombre} NO se inicia en el nodo {_NODO_ID}: {_motivo_nodo}")
                    continue
                usuario, contrasena = obtener_credenciales(nombre)
                if not usuario:
                    _safe_send(chat_id, f"❌ Sin credenciales para *{nombre}*.",
                               parse_mode="Markdown"); continue
                if nombre in _sesiones_activas:
                    _sesiones_activas[nombre].stop()
                cuentas_a_iniciar.append((nombre, usuario, contrasena))
            if not cuentas_a_iniciar:
                enviar_menu(chat_id, "ℹ️ No hay cuentas nuevas que iniciar — todas ya están activas.")
                return
            monitors = {}
            for nombre, usuario, contrasena in cuentas_a_iniciar:
                monitor = MonitorThread(nombre, usuario, contrasena, chat_id)
                monitor.start()
                _sesiones_activas[nombre] = monitor
                monitors[nombre] = monitor
            _actualizar_conteo()

            nombres_ok: list = []
            _lock_nombres = threading.Lock()

            def _esperar_y_notificar(nom: str, mon):
                # 360s: cubre hasta 8 esperas de RAM (64s) + login + nav + primera API
                mon.startup_done.wait(timeout=360)
                if not mon.startup_ok:
                    _enviar(chat_id, f"❌ *{nom}*: no se pudo iniciar sesión.",
                            parse_mode="Markdown"); return
                mon.primera_data_event.wait(timeout=150)
                try:
                    saldo_str = mon.primera_data_saldo or mon.saldo_inicial or "—"
                    txs       = mon.primera_data_txs   # todos los de hoy, sin límite
                    cabecera  = f"✅ *{nom}*\n💰 Saldo: `{saldo_str}`"
                    if txs:
                        _LIMITE   = 4000   # margen bajo el límite de 4096 de Telegram
                        # Ordenar de mayor a menor monto absoluto (solo para el mensaje,
                        # no modifica la lista original ni ningún estado interno).
                        txs_ordenadas = sorted(txs, key=lambda t: abs(float(t.get("amount", 0))), reverse=True)
                        lineas = []
                        for i, tx in enumerate(txs_ordenadas, 1):
                            desc   = MonitorThread._simplificar_desc_movimiento((tx.get("description") or "—").strip())[:30]
                            amount = float(tx.get("amount", 0))
                            em     = "🔴" if amount < 0 else "🟢"
                            monto  = MonitorThread._format_monto(amount)
                            lineas.append(f"{i}. {em} {desc} — `{monto}`")
                        # Partir en bloques que quepan en un mensaje de Telegram
                        # 18/09/2026: la fecha ya no va en cada línea (todas son
                        # de HOY, era redundante repetirla) — va una sola vez acá.
                        fecha_hoy = MonitorThread._format_fecha_larga_hoy()
                        bloques  = []
                        bloque   = cabecera + f"\n📋 *Movimientos de hoy — {fecha_hoy}:*"
                        for linea in lineas:
                            if len(bloque) + len(linea) + 1 > _LIMITE:
                                bloques.append(bloque)
                                bloque = linea
                            else:
                                bloque += "\n" + linea
                        bloques.append(bloque)
                        for bloque in bloques:
                            _enviar(chat_id, bloque, parse_mode="Markdown")
                    else:
                        _enviar(chat_id,
                                cabecera + "\n_Sin movimientos hoy\\._",
                                parse_mode="Markdown")
                except Exception as _e_notif:
                    print(f"[{nom}] ⚠️ Error al formatear notificación de inicio: {_e_notif}")
                    _enviar(chat_id, f"✅ *{nom}* — sesión iniciada.", parse_mode="Markdown")
                with _lock_nombres:
                    nombres_ok.append(nom)

            watchers = []
            for nom, mon in monitors.items():
                t = threading.Thread(target=_esperar_y_notificar, args=(nom, mon), daemon=True)
                t.start()
                watchers.append(t)
            for t in watchers:
                t.join()
            if nombres_ok:
                _enviar(
                    chat_id,
                    f"👁️ Monitoreando: *{', '.join(sorted(nombres_ok))}*\n\n"
                    f"📌 Cada pago se notifica individualmente.\n"
                    f"⚠️ Si necesitas entrar a Bancolombia, usa *⏸ Pausar cuenta*.",
                    parse_mode="Markdown"
                )
                enviar_menu(chat_id)

        threading.Thread(target=_arrancar, daemon=True).start()
        return

    if call.data.startswith("mon_activa_noop:"):
        nombre_activa = call.data[len("mon_activa_noop:"):]
        _ack(f"🟢 {nombre_activa} ya está activa y seguirá corriendo.")
        return

    if not estado:
        # Estado expirado: recargar solo si nadie más ya lo seteó.
        # IMPORTANTE: no sobreescribir si el usuario ya estaba seleccionando.
        _ack("🔄 Cargando menú...")
        def _recargar_estado():
            # Doble-chequeo: si entre tanto otro hilo ya seteó el estado, no tocar nada.
            if _estado_seleccion.get(chat_id):
                return
            cuentas, error = listar_cuentas_bw()
            if error or not cuentas:
                _safe_send(chat_id, "❌ No se pudieron cargar las cuentas. Usa /menu.",
                           parse_mode="Markdown")
                return
            cuentas, _ocultas = _nodos.filtrar_manual(cuentas)
            if not cuentas:
                _safe_send(chat_id, f"⛔ Ninguna cuenta de Bitwarden pertenece al nodo {_NODO_ID}.")
                return
            # Tercer chequeo justo antes de escribir
            if _estado_seleccion.get(chat_id):
                return
            ya_activas = {n for n, m in _sesiones_activas.items() if m.is_alive()}
            _estado_seleccion[chat_id] = {
                "cuentas":       cuentas,
                "seleccionadas": set(),
                "ya_activas":    ya_activas,
            }
            _safe_send(
                chat_id,
                f"📋 *{len(cuentas)} cuenta(s) disponibles.*\n"
                "Selecciona las que quieres monitorear:",
                reply_markup=_teclado_seleccion(cuentas, set(), ya_activas),
                parse_mode="Markdown"
            )
        threading.Thread(target=_recargar_estado, daemon=True).start()
        return
    nombre     = call.data[len("mon_toggle:"):]
    ya_activas = estado.get("ya_activas", set())
    if nombre in ya_activas:
        _ack(f"🟢 {nombre} ya está activa — no se puede re-seleccionar aquí."); return
    if nombre in estado["seleccionadas"]:
        estado["seleccionadas"].discard(nombre)
    else:
        estado["seleccionadas"].add(nombre)
    _ack()
    try:
        bot.edit_message_reply_markup(
            chat_id, call.message.message_id,
            reply_markup=_teclado_seleccion(
                estado["cuentas"], estado["seleccionadas"], ya_activas
            )
        )
    except Exception:
        pass

@bot.message_handler(commands=['detener'])
def handler_detener(message):
    if not _sesiones_activas:
        _safe_send(message.chat.id, "ℹ️ No hay sesiones activas."); return
    nombres = sorted(_sesiones_activas.keys())
    for m in _sesiones_activas.values():
        m.stop()
    _sesiones_activas.clear()
    _actualizar_conteo()
    _safe_send(message.chat.id, "🛑 Monitoreo detenido: " + ", ".join(nombres))
    enviar_menu(message.chat.id)

@bot.message_handler(commands=['estado'])
def handler_estado(message):
    if not _sesiones_activas:
        _safe_send(message.chat.id, "ℹ️ Sin sesiones activas."); return
    lineas = ["📊 *Sesiones activas (2.0):*"]
    for nombre, m in sorted(_sesiones_activas.items()):
        vivo   = "✅" if m.is_alive() else "❌"
        estado = "⏸ Pausado" if m.esta_pausado else "▶️ Activo"
        saldo  = m.saldo_actual or "—"
        lineas.append(f"{vivo} *{nombre}* — {estado} — `{saldo}` — {len(m.txs_conocidas)} txs")
    _safe_send(message.chat.id, "\n".join(lineas), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # ── Una sola instancia por nodo ─────────────────────────────────────────
    # Dos iniciar.py a la vez = cada cuenta con dos navegadores, el doble de
    # RAM y logins duplicados (el banco lo castiga con bloqueo temporal). Un
    # mutex con nombre lo impide aunque se lancen desde carpetas distintas.
    _mutex_instancia = None
    if os.name == "nt":
        try:
            import ctypes as _ct
            _id_nodo = str(_NODO_ID)
            try:
                import config as _cfg_mx
                _id_nodo = str(getattr(_cfg_mx, "NODO_ID", _id_nodo))
            except Exception:
                pass
            _mutex_instancia = _ct.windll.kernel32.CreateMutexW(
                None, False, f"BancolombiaBot_Nodo{_id_nodo}")
            if _ct.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
                print("⛔ Ya hay otra instancia de iniciar.py corriendo en este PC.")
                print("   Ciérrala antes de abrir otra: dos instancias duplican")
                print("   sesiones, RAM y logins (y el banco bloquea la IP).")
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as _e_mx:
            print(f"⚠️ No pude comprobar si ya hay otra instancia: {_e_mx}")

    print("\n" + "=" * 55)
    print("🚀 BOT BANCOLOMBIA 2.0 — SISTEMA MODULAR")
    print("=" * 55 + "\n")

    # ── Huella de configuración ────────────────────────────────────────────
    # El log del 10/11 16:02 mezclaba "Abriendo Chromium + CapSolver" (código
    # nuevo) con "Abriendo Edge..." en el mismo rato. Eso solo pasa si hay dos
    # procesos o si se está ejecutando OTRA copia de la carpeta (con otro
    # config.py). Esta línea deja por escrito qué archivo y qué valores se
    # cargaron, para no volver a adivinarlo.
    try:
        import config as _cfg_boot
        print(f"⚙️  config: {os.path.abspath(_cfg_boot.__file__)}")
        print(f"   NODO_ID={getattr(_cfg_boot, 'NODO_ID', '?')} | "
              f"NAVEGADOR={getattr(_cfg_boot, 'NAVEGADOR', '?')} | "
              f"CAPSOLVER_DESDE_INICIO={getattr(_cfg_boot, 'CAPSOLVER_DESDE_INICIO', False)} | "
              f"USAR_PROXY_VPN={getattr(_cfg_boot, 'USAR_PROXY_VPN', False)}")
        print(f"   Proxy: {getattr(_cfg_boot, 'PROXY_VPN_SERVER', '') or 'sin proxy'}")
    except Exception as _e_boot:
        print(f"⚠️ No pude leer config.py para la huella de arranque: {_e_boot}")


    # Modo simulación: letrero grande e inconfundible (ver simulacion.py).
    try:
        import simulacion as _sim
        _sim.banner()
        if _sim.activo() and not _sim.pruebas_activo():
            print("\n⛔ MODO SIMULACIÓN sin MODO_PRUEBAS=True.")
            print("   Para no mandar NADA a los bots reales por error, el sistema")
            print("   no arranca. Pon MODO_PRUEBAS = True en config.py y reintenta.")
            sys.exit(1)
    except SystemExit:
        raise
    except Exception:
        pass

    # Limpiar caché de movimientos al iniciar (se reconstruye desde Bancolombia)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        borrados = 0
        for f in os.listdir(CACHE_DIR):
            if f.startswith("cache2_") and f.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, f)); borrados += 1
                except Exception:
                    pass
        print(f"🗑️  Caché limpiada: {borrados} archivo(s)" if borrados else "✅ Caché limpia")
    except Exception as e:
        print(f"⚠️ No se pudo limpiar caché: {e}")

    # Limpiar snapshots diarios previos (se regeneran bajo demanda)
    try:
        from movimiento import MovimientoMixin
        diario_dir = MovimientoMixin.DIARIO_DIR
        os.makedirs(diario_dir, exist_ok=True)
        borrados_d = 0
        for f in os.listdir(diario_dir):
            if f.startswith("diario_") and f.endswith(".json"):
                try:
                    os.remove(os.path.join(diario_dir, f)); borrados_d += 1
                except Exception:
                    pass
        if borrados_d:
            print(f"🗑️  Snapshots diarios eliminados: {borrados_d}")
    except Exception as e:
        print(f"⚠️ No se pudo limpiar movimientos_diarios: {e}")

    # Limpiar caché de Chrome (Cache, GPUCache, etc.)
    _SUBDIRS = ("Cache","Code Cache","GPUCache","ShaderCache",
                "Service Worker","DawnCache","blob_storage")
    mb = 0
    try:
        base = os.path.dirname(USER_DATA_DIR)
        for entrada in os.listdir(base):
            if not entrada.startswith("Monitor_"): continue
            default = os.path.join(base, entrada, "Default")
            if not os.path.isdir(default): continue
            for sub in _SUBDIRS:
                ruta = os.path.join(default, sub)
                if os.path.isdir(ruta):
                    try:
                        tam = sum(
                            os.path.getsize(os.path.join(dp, f))
                            for dp, _, fs in os.walk(ruta) for f in fs
                            if os.path.exists(os.path.join(dp, f))
                        )
                        shutil.rmtree(ruta, ignore_errors=True)
                        mb += tam / (1024 * 1024)
                    except Exception:
                        pass
        print(f"🧹 Chrome: {mb:.1f} MB liberados" if mb > 0 else "✅ Chrome limpio")
    except Exception:
        pass

    # Nodo secundario: mostrar QUÉ bot de Telegram usa, para no equivocarse de token.
    if _NODO_ID != "A":
        try:
            print(f"🖥️ Nodo {_NODO_ID} — bot de Telegram: @{bot.get_me().username} "
                  f"(debe ser el MISMO que usa el nodo A; este nodo no tiene menú)")
        except Exception as _e:
            print(f"⚠️ No se pudo leer el bot de Telegram del nodo {_NODO_ID}: {_e}")

    # ── Paso 0: eliminar webhook si estaba configurado ──────────────
    # Si el bot tiene un webhook activo, los callback_query van al webhook
    # y nunca llegan al polling local → los botones no responden.
    try:
        if not _SIN_MENU_TELEGRAM:
            bot.delete_webhook(drop_pending_updates=False)
            print("🔗 Webhook eliminado — modo polling activo")
    except Exception as _e:
        print(f"⚠️ delete_webhook: {_e}")

    # ── Paso 1: descartar callbacks acumulados de sesiones anteriores ──
    # Hay que hacerlo ANTES de setear el estado para no procesar clicks
    # de menus viejos que llegarían justo cuando el estado ya esté listo.
    try:
        updates = [] if _SIN_MENU_TELEGRAM else bot.get_updates(timeout=3)
        if updates:
            bot.get_updates(offset=updates[-1].update_id + 1, timeout=1)
            print(f"🧹 {len(updates)} update(s) antiguo(s) descartado(s)")
    except Exception:
        pass

    # ── Paso 2: enviar menú principal a todos los chats ───────────────
    # Solo el menú principal — igual que bancolombia.py.
    # El menú de selección de cuentas aparece cuando el usuario clickea
    # "🚀 Iniciar monitoreo" → _cargar() lo genera en ese momento.
    # Esto evita: (a) 30-60 s de espera a BW en startup,
    #             (b) doble menú cuando el usuario también clickea "Iniciar".
    if _SIN_MENU_TELEGRAM:
        print(f"🟢 Nodo {_NODO_ID} listo — sin menú de Telegram (solo órdenes de Pochonance)")
    else:
        print("🟢 Bot listo — enviando menú a chats registrados...")
    for chat_id in ([] if _SIN_MENU_TELEGRAM else _leer_chat_ids()):
        _safe_send(
            chat_id,
            "🟢 *Bot Bancolombia 2.0 iniciado.*\n\n¿Qué deseas hacer?",
            reply_markup=_teclado_menu_principal(),
            parse_mode="Markdown"
        )
    if not _SIN_MENU_TELEGRAM:
        print("✅ Menú enviado — arrancando polling")

    # ── Paso 3: escuchar activaciones de Pochonance en segundo plano ──
    # Comparte _sesiones_activas/_actualizar_conteo con el flujo de Telegram
    # de arriba — una cuenta activada desde Pochonance queda visible en el
    # menú de Telegram y viceversa, nunca hay dos monitores para la misma cuenta.
    # En simulación NO se arranca el activador: es quien pedía abrir cuentas
    # a Railway real. Así nada toca el backend de producción. Las cuentas se
    # abren a mano desde el menú del bot ("🚀 Iniciar monitoreo").
    try:
        import simulacion as _sim_chk
        _sim_off = _sim_chk.activo()
    except Exception:
        _sim_off = False
    if _sim_off:
        print("🧪 SIMULACIÓN: activador de Pochonance NO arrancado.")
        # Abrir SOLO las cuentas de la lista (sin menú, sin Pochonance).
        try:
            from Pochonance.pochonance_activador import _arrancar_cuenta
            _cuentas_sim = _sim_chk.cuentas_a_abrir()
            print(f"🧪 SIMULACIÓN: abriendo {len(_cuentas_sim)} cuenta(s): "
                  f"{', '.join(_cuentas_sim)}")
            def _abrir_cuentas_sim():
                for _n in _cuentas_sim:
                    try:
                        _arrancar_cuenta(_n, _sesiones_activas, _actualizar_conteo)
                    except Exception as _e:
                        print(f"🧪 SIMULACIÓN: no se pudo abrir '{_n}': {_e}")
                    time.sleep(1)   # el turno de arranque las escalona solas
            threading.Thread(target=_abrir_cuentas_sim, daemon=True,
                             name="sim-abrir").start()
        except Exception as _e:
            print(f"🧪 SIMULACIÓN: error abriendo cuentas: {_e}")
    else:
        iniciar_polling_activacion(_sesiones_activas, _actualizar_conteo)

    # ── Consumo del PC cada minuto (para medir pruebas de rendimiento) ──
    # Leer CPU/RAM de Windows tarda < 1 ms; hilo aparte, nunca frena cuentas.
    def _log_consumo_pc():
        try:
            import psutil
        except Exception:
            print("⚠️ psutil no disponible — sin línea de consumo del PC")
            return
        psutil.cpu_percent(interval=None)   # la primera lectura siempre da 0
        while True:
            time.sleep(60)
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                vivas = sum(1 for m in list(_sesiones_activas.values()) if m.is_alive())
                usada, total = (mem.total - mem.available) / 1024**3, mem.total / 1024**3
                print(f"🖥️ PC: CPU {cpu:.0f}% | RAM {usada:.1f}/{total:.1f} GB "
                      f"({mem.percent:.0f}%) | sesiones activas: {vivas}".replace(".", ","))
            except Exception:
                pass
    threading.Thread(target=_log_consumo_pc, daemon=True, name="consumo-pc").start()

    # ── Modo simulación: publicar qué cuentas están activas cada 5 s ──
    # Para que inyeccion.py sepa a cuáles mandar movimientos (ver simulacion.py).
    try:
        import simulacion as _sim_mod
        if _sim_mod.activo():
            def _publicar_activas():
                while True:
                    try:
                        vivas = [n for n, m in list(_sesiones_activas.items()) if m.is_alive()]
                        _sim_mod.publicar_activas(vivas)
                    except Exception:
                        pass
                    time.sleep(5)
            threading.Thread(target=_publicar_activas, daemon=True, name="sim-activas").start()
    except Exception:
        pass

    # Polling con reintentos (none_stop=False evita el CPU spin de infinity_polling)
    while True:
        try:
            if _SIN_MENU_TELEGRAM:
                time.sleep(3600)   # sin polling de Telegram; los hilos de Pochonance siguen
            else:
                bot.polling(none_stop=False, timeout=30, long_polling_timeout=20)
        except (KeyboardInterrupt, SystemExit):
            print("\nBot detenido manualmente.")
            for _m in list(_sesiones_activas.values()):
                try: _m.stop()
                except Exception: pass
            break
        except Exception as e_poll:
            print(f"⚠️ Polling Telegram caído: {e_poll} — reintentando en 10s...")
            try:
                time.sleep(10)
            except (KeyboardInterrupt, SystemExit):
                break
