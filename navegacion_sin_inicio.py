# -*- coding: utf-8 -*-
# =====================================================================
# NAVEGACION_SIN_INICIO.PY  (PRUEBA — 29/09/2026)
# Recorrido de cada ciclo SIN pasar por la página de Inicio:
#   Productos → Tus cuentas → Detalles → Movimientos
#
# Por qué: la memoria JS de la página de Bancolombia crece con cada
# recorrido del menú (no es la telemetría: bloquear Dynatrace/VWO/Singular
# no cambió nada). La página de Inicio suele ser la más pesada (ofertas,
# carruseles, experimentos) y su paso es de los que más se alarga.
#
# Cómo: NO copia el recorrido. Llama al _click_buscar de siempre
# (movimiento.py, sin modificar) con una "vista" de la página donde los
# selectores del botón Inicio aparecen como no visibles → ese paso se salta
# solo (en la línea 🔄 se verá "inicio 0.0") y todo lo demás es idéntico.
#
# RECUPERACIÓN AUTOMÁTICA: si sin Inicio Bancolombia no responde por la API
# en ESPERA_API_S segundos (datos en caché), se hace en ese mismo ciclo el
# recorrido completo con Inicio — la cuenta nunca queda ciega.
#
# ACTIVAR / DESACTIVAR: USAR_NAV_SIN_INICIO en sesiones.py.
# =====================================================================
from movimiento import MovimientoMixin

# Cuentas donde se aplica (nombre sin espacios al final). {"*"} = todas.
CUENTAS_SIN_INICIO: set = {"*"}
ESPERA_API_S = 6.0

# Los mismos selectores que usa _click_buscar (movimiento.py) para "Inicio"
SELECTORES_INICIO = {
    "a[href*='/home']", "[routerLink='/home']",
    "a:has-text('Inicio')", "mat-list-item:has-text('Inicio')",
    "span:has-text('Inicio')",
}


class _LocatorInvisible:
    """Locator falso: nunca está visible, así el clic en Inicio no ocurre."""
    @property
    def first(self):
        return self

    def is_visible(self, *args, **kwargs):
        return False


class _PaginaSinInicio:
    """Envuelve la página real; solo oculta los selectores de Inicio."""
    def __init__(self, page):
        self._page = page

    def locator(self, selector, *args, **kwargs):
        if selector in SELECTORES_INICIO:
            return _LocatorInvisible()
        return self._page.locator(selector, *args, **kwargs)

    def __getattr__(self, nombre):
        return getattr(self._page, nombre)


class MovimientoSinInicioMixin(MovimientoMixin):

    def _click_buscar(self, page) -> bool:
        nombre = self.nombre.strip()
        if "*" not in CUENTAS_SIN_INICIO and nombre not in CUENTAS_SIN_INICIO:
            return super()._click_buscar(page)

        if not getattr(self, "_aviso_sin_inicio", False):
            self._aviso_sin_inicio = True
            print(f"[{self.nombre}] 🧪 Prueba sin Inicio activa "
                  f"(en la línea 🔄, 'inicio 0.0' = paso saltado)")

        ok = super()._click_buscar(_PaginaSinInicio(page))
        if not ok:
            return self._recorrido_con_inicio(page, "falló sin Inicio")

        txs = self._esperar_api(timeout=ESPERA_API_S, page=page)
        if txs is None:
            return self._recorrido_con_inicio(
                page, f"sin respuesta API en {ESPERA_API_S:.0f}s")
        # Dejar la respuesta en la cola: sesiones.py la toma con _esperar_api
        self._tx_api_queue.put(txs)
        return True

    def _recorrido_con_inicio(self, page, motivo: str) -> bool:
        self._fallos_sin_inicio = getattr(self, "_fallos_sin_inicio", 0) + 1
        print(f"[{self.nombre}] ↩️ Sin Inicio: {motivo} — recorrido completo con Inicio "
              f"(fallos sin Inicio en esta cuenta: {self._fallos_sin_inicio})")
        return super()._click_buscar(page)
