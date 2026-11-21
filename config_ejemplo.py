# -*- coding: utf-8 -*-
# ================================================================
# CONFIG_EJEMPLO.PY — Plantilla de configuracion
# Copia este archivo como  config.py  y llena los valores reales.
# config.py esta en .gitignore y NUNCA se sube al repositorio.
# ================================================================

# ── Telegram ──────────────────────────────────────────────────────
TOKEN_TELEGRAM  = "TOKEN_DEL_BOT_AQUI"         # BotFather → /mybots → API Token
OWNER_CHAT_ID   = 0                             # Tu chat_id personal (entero)
FULL_NOTIF_IDS      = {0}                       # Chat IDs que reciben notif. completa
EXTRA_COMPACT_IDS   = {0}                       # Chat IDs que SIEMPRE reciben notif. compacta vía Bot Saldo (respaldo Pochonance)

# ── Bitwarden ─────────────────────────────────────────────────────
BW_CLIENT_ID     = "user.XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
BW_CLIENT_SECRET = "TU_CLIENT_SECRET_AQUI"
BW_MASTER_PASS   = "TU_MASTER_PASSWORD_AQUI"

# ── Pochonance (canal directo bot → plataforma) ───────────────────
# Dejar vacío ("") para desactivar el bridge sin afectar Telegram.
POCHONANCE_URL     = ""          # ej: "https://pochonance.com/api/movimiento"
POCHONANCE_API_KEY = ""          # Bearer token que Pochonance emita

# ── Escenario 1 — Fallback directo Cuentas P2P ───────────────────
# Token del bot "Cuentas P2P". Se usa cuando Pochonance no responde
# para notificar directamente a los chat_ids sin pasar por Railway.
# Dejar vacío ("") para deshabilitar el fallback.
TOKEN_CUENTAS_P2P = ""

# ── Modo simulación (pruebas de carga — ver simulacion.py) ────────
# Con esto encendido, el sistema corre normal (Chrome real) pero CORTA
# todo contacto con Railway (bridge + activador), y las cuentas se abren
# a mano desde el menú del bot. EXIGE MODO_PRUEBAS=True (bots de prueba);
# si no, el sistema se niega a arrancar.
# Se recomienda encenderlo con la variable de entorno en PowerShell:
#   $env:MOVS_SIMULACION="1"   (en vez de aquí, para no dejarlo prendido)
MODO_SIMULACION  = False

# ── Varios PCs corriendo Movimientos (nodos) ──────────────────────
# NODO_ID identifica este PC ante el backend de Pochonance. "A" = el PC
# original (se comporta exactamente como siempre). Un segundo PC usa "B".
NODO_ID = "A"
# IDs de Pochonance (columna id de account_cop, NO el de Bitwarden) que ESTE nodo
# atiende. Ej.: el A "1-30" y el B "31-" (31 en adelante). Cada orden de Pochonance
# trae el ID de la cuenta y el nodo SOLO la abre si ese ID es suyo; si no, avisa
# al dueño y no toca nada. Sin esto el nodo rechaza TODO a propósito. Ver nodos.py.
NODO_IDS_PROPIOS = "1-30"
# Un nodo distinto de "A" NO consulta las colas de Pochonance mientras esto
# sea False. Ponerlo True SOLO cuando el backend ya enruta por nodo; si no,
# ese PC le robaría las órdenes al nodo A y corrompería el latido.
BACKEND_SOPORTA_NODOS = False
# Clave de GET /conciliacion/pendiente (app.conciliacion.api-key en el
# backend). La de /movimientos/* es POCHONANCE_API_KEY (arriba).
POCHONANCE_API_KEY_CONCILIACION = ""

# Navegador con el que se abre Bancolombia: "msedge" (Edge instalado), "chrome" (Google Chrome instalado)
# o "chromium" (el de Playwright, igual que el Nodo A). Cada uno usa su propia carpeta de perfiles.
NAVEGADOR = "msedge"

# ── Arranque directo en Chromium + extensión CapSolver ───────────────
# True  → todas las sesiones abren desde el inicio en el Chromium de
#         Playwright con capsolver_extension montada (sin primer intento en
#         Edge). Es el único navegador que permite montar extensiones.
# False → comportamiento anterior de dos carriles (Edge primero y, si
#         aparece reCAPTCHA, relanza con Chromium + extensión).
CAPSOLVER_DESDE_INICIO = True

# ── VPN / proxy de salida (SOLO pruebas) ─────────────────────────────
# Hace que únicamente el navegador de Bancolombia salga por un proxy local;
# el resto del PC (Telegram, Pochonance, CapSolver) sigue igual.
# Preparado para Cloudflare WARP en modo proxy (ver vpn_warp.py).
#   python vpn_warp.py          -> configura WARP, conecta y verifica la IP
#   python vpn_warp.py estado   -> muestra el estado
#   python vpn_warp.py apagar   -> desconecta WARP
# Si el proxy no responde, el navegador abre SIN VPN y solo deja un aviso.
USAR_PROXY_VPN    = False
PROXY_VPN_SERVER  = "http://127.0.0.1:40000"   # WARP en modo proxy escucha aquí
PROXY_VPN_USUARIO = ""                          # normalmente vacío en WARP
PROXY_VPN_CLAVE   = ""

# ---- Reinicio por lentitud (sesiones.py) ----
# Si se usa proxy/VPN, la navegacion normal sube a 15-27 s y el umbral de 12 s
# hace que el bot reinicie cuentas una y otra vez (cada reinicio = un login
# nuevo = riesgo de bloqueo temporal). Subir el umbral y exigir mas ciclos
# lentos seguidos.
# NAV_LENTA_S      = 25
# NAV_LENTA_CICLOS = 5
# NAV_CRITICA_S    = 45

# CapSolver: API key (secreto). La extension la recibe al arrancar: se genera capsolver_extension/assets/config.js.
CAPSOLVER_API_KEY = ""
