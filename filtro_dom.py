# -*- coding: utf-8 -*-
# =====================================================================
# FILTRO_DOM.PY  (29/09/2026)
# Evita el spam de "⚠️ movimiento vía DOM" con filas históricas.
#
# Problema: el respaldo por tabla (_fallback_dom en movimiento.py) marca
# como nueva toda fila que no esté en su "foto" de la tabla. Si esa foto
# está vacía (al arrancar solo se vio el calendario), las 20 filas visibles
# parecen nuevas → 20 notificaciones de movimientos de ayer y de hoy.
# Caso real: un titular, saldo $513.085 → $848.035 (Δ $334.950) y se
# notificaron 20 filas cuando la única nueva era la de $334.950.
#
# Regla: un movimiento nuevo tiene que CUADRAR con el cambio de saldo.
#   - Foto confiable y pocas filas nuevas (≤ MAX_FILAS_CONFIABLES) → se
#     dejan como están (varias txs reales a la vez siguen funcionando).
#   - Foto vacía, o demasiadas filas "nuevas" de golpe → solo pasan las
#     filas (1 a 3, prefiriendo las de hoy) cuyos montos suman el Δ del
#     saldo. Si ninguna combinación cuadra, no pasa ninguna: sesiones.py
#     manda entonces UN solo aviso de cambio de saldo en vez de N filas.
# =====================================================================
import re
from datetime import datetime
from itertools import combinations

from notificar import _parsear_pesos

MAX_FILAS_CONFIABLES = 3   # más filas "nuevas" que esto de golpe = sospechoso
MAX_COMBINACION      = 3   # hasta cuántas filas se combinan para cuadrar el Δ

_MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
          "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
_RE_FECHA = re.compile(r"(\d{1,2})\s+([a-záéíóú]{3,})\.?\s+(\d{4})", re.I)
_RE_MONTO = re.compile(r"(-?)\s*\$\s*([\d.]+(?:,\d{1,2})?)")


def monto_fila(fila: str) -> float | None:
    """Último monto de la fila ('-$ 4.999.999,00' → -4999999.0)."""
    montos = _RE_MONTO.findall(fila or "")
    if not montos:
        return None
    signo, numero = montos[-1]
    valor = _parsear_pesos(numero)
    if valor is None:
        return None
    return -abs(valor) if signo == "-" else valor


def partes_fila(fila: str) -> dict:
    """
    Separa una fila de la tabla en sus partes para armar una notificación normal.
    '02 oct 2026 | 02 oct 2026 | TRANSFERENCIA CTA SUC VIRTUAL | 10000000001 | $ 250.000,00'
      → {fecha: '02 oct 2026', desc: 'TRANSFERENCIA CTA SUC VIRTUAL',
         ref: '10000000001', monto: 250000.0}
    """
    fecha, textos = "", []
    for parte in (p.strip() for p in (fila or "").split("|")):
        if not parte:
            continue
        if _RE_FECHA.fullmatch(parte):
            fecha = fecha or parte
        elif not _RE_MONTO.search(parte):
            textos.append(parte)
    return {
        "fecha": fecha,
        "desc":  textos[0] if textos else "—",
        "ref":   textos[1] if len(textos) > 1 else "",
        "monto": monto_fila(fila),
    }


def es_de_hoy(fila: str) -> bool:
    m = _RE_FECHA.search(fila or "")
    if not m:
        return False
    mes = _MESES.get(m.group(2).lower()[:3])
    hoy = datetime.now()
    return (mes == hoy.month and int(m.group(1)) == hoy.day
            and int(m.group(3)) == hoy.year)


def filtrar_filas_por_saldo(nuevas: list, saldo_anterior: str, saldo_actual: str,
                            sin_foto: bool) -> tuple:
    """
    Devuelve (filas_que_pasan, motivo_del_descarte | None).
    """
    if not nuevas:
        return nuevas, None
    if not sin_foto and len(nuevas) <= MAX_FILAS_CONFIABLES:
        return nuevas, None

    val_ant = _parsear_pesos(saldo_anterior or "")
    val_act = _parsear_pesos(saldo_actual or "")
    if val_ant is None or val_act is None:
        return [], f"{len(nuevas)} fila(s) sin saldo con qué cuadrarlas"
    delta = val_act - val_ant

    de_hoy = [f for f in nuevas if es_de_hoy(f)]
    for grupo in (de_hoy, nuevas):
        candidatas = [(f, monto_fila(f)) for f in grupo]
        candidatas = [(f, m) for f, m in candidatas if m is not None]
        for k in range(1, MAX_COMBINACION + 1):
            for combo in combinations(candidatas, k):
                if abs(sum(m for _, m in combo) - delta) < 1:
                    return [f for f, _ in combo], None

    return [], (f"{len(nuevas)} fila(s) no cuadran con el cambio de saldo "
                f"(Δ {delta:+,.0f})")
