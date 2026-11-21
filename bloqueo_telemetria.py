# -*- coding: utf-8 -*-
# =====================================================================
# BLOQUEO_TELEMETRIA.PY  (PRUEBA — 29/09/2026)
# Bloquea en Chrome los servicios de telemetría/analítica de terceros que
# carga la página de Bancolombia y que NO son parte de su seguridad.
#
# Por qué: la memoria JS de la página crece sin parar (100→200 MB en ~4 min)
# y cada clic se vuelve 6-8× más lento. Las líneas 🔬 mostraron que la
# página habla todo el tiempo con Dynatrace (monitoreo de usuario: registra
# cada clic y cambio de pantalla), VWO (pruebas A/B y mapas de calor) y
# Singular (analítica de marketing) — sospechosos clásicos de esa fuga.
#
# Cómo se usa: sesiones.py llama a es_telemetria_bloqueada() dentro de su
# filtro de requests (_bloquear). Es lo único que toca de la lógica
# principal. Para apagar la prueba: CUENTAS_CON_BLOQUEO = set().
#
# Qué comparar: las líneas 🔬 (JS heap) y la duración de "nav" de las
# cuentas con bloqueo contra las demás, durante 10-15 minutos.
# =====================================================================

# Cuentas donde se aplica (nombre tal como sale en la terminal, sin
# espacios al final). {"*"} = todas. set() = prueba apagada.
CUENTAS_CON_BLOQUEO: set = set()   # 29/09 17:05: bloquear no frenó la fuga → apagado

# Fragmentos de URL que se bloquean. Solo analítica/telemetría de terceros.
PATRONES_TELEMETRIA = (
    # Dynatrace — agente RUM (script) y envío de datos (beacons)
    "dynatrace.com",
    "ruxitagentjs",
    "ruxitagent_",
    "dtagent",
    # VWO (Visual Website Optimizer) — pruebas A/B, mapas de calor
    "visualwebsiteoptimizer.com",
    "wingify.com",
    # Singular — analítica de marketing
    "singular.net",
)

# Primer bloqueo por (cuenta, patrón) → se avisa una vez en la terminal
_avisados: set = set()


def es_telemetria_bloqueada(cuenta: str, url: str) -> bool:
    """True si la URL es telemetría de terceros y la cuenta está en la prueba."""
    nombre = (cuenta or "").strip()
    if "*" not in CUENTAS_CON_BLOQUEO and nombre not in CUENTAS_CON_BLOQUEO:
        return False
    url = url.lower()
    for patron in PATRONES_TELEMETRIA:
        if patron in url:
            if (nombre, patron) not in _avisados:
                _avisados.add((nombre, patron))
                print(f"[{nombre}] 🛡️ Telemetría bloqueada ({patron}): {url[:90]}")
            return True
    return False
