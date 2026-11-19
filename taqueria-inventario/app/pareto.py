"""
80/20 (analisis ABC) de insumos, pedido por los duenos: saber cuales pocos
insumos se llevan la mayor parte del gasto, para rastrear la merma a detalle
solo de esos y no gastar tiempo contando cebollas.

Se calcula sobre las COMPRAS REALES registradas en el modulo de Compras:
  - Por costo: cuanto dinero se fue en cada insumo en el periodo.
  - Por cantidad: cuanto se compro de cada insumo, comparando solo dentro de
    la misma unidad (kg con kg, piezas con piezas) -- sumar kilos con piezas
    no significaria nada.

Clases:
  A = los insumos que juntos llegan al 80% (incluido el que cruza el 80%)
  B = los siguientes hasta el 95%
  C = el resto

Los precios de referencia con los que se sembro el modulo de Compras
(catalogo del proveedor y precios de la duena anterior) NO son compras: no
dicen cuanto se compro, solo cuanto cuesta. Se excluyen siempre, para no
inventar un gasto que no ocurrio.
"""
from __future__ import annotations

from . import compras

CORTE_A = 80.0
CORTE_B = 95.0


def _notas_de_referencia() -> set[str]:
    return {c.get("notas", "") for c in compras._compras_iniciales() if c.get("notas")}


def _clasificar(filas: list[dict], campo: str) -> list[dict]:
    """Ordena de mayor a menor por `campo` y agrega %, % acumulado y clase."""
    filas = sorted(filas, key=lambda f: f[campo], reverse=True)
    total = sum(f[campo] for f in filas)
    acumulado_previo = 0.0
    for f in filas:
        pct = 100 * f[campo] / total if total else 0.0
        # La clase se decide por donde EMPIEZA el insumo: si antes de el
        # todavia no se llegaba al 80%, es A (asi el que cruza el 80% es A).
        if acumulado_previo < CORTE_A:
            clase = "A"
        elif acumulado_previo < CORTE_B:
            clase = "B"
        else:
            clase = "C"
        acumulado_previo += pct
        f["pct"] = round(pct, 1)
        f["pct_acumulado"] = round(min(acumulado_previo, 100.0), 1)
        f["clase"] = clase
    return filas


def analisis(desde: str | None = None, hasta: str | None = None, sucursal: str | None = None) -> dict:
    todas = compras.listar_compras(desde=desde, hasta=hasta, sucursal=sucursal)
    referencia = _notas_de_referencia()
    reales = [c for c in todas if (c.get("notas") or "") not in referencia]

    por_costo: dict[str, dict] = {}
    por_cantidad: dict[tuple[str, str], dict] = {}
    sin_unidad: dict[str, int] = {}

    for c in reales:
        clave = compras._normalizar(c["ingrediente"])
        f = por_costo.setdefault(clave, {"insumo": c["ingrediente"].strip(), "gasto": 0.0, "compras": 0})
        f["gasto"] += c["precio_total"] or 0.0
        f["compras"] += 1

        familia = compras._familia_unidad(c["unidad"])
        if c["cantidad"] and familia:
            unidad_base, factor = familia
            q = por_cantidad.setdefault((unidad_base, clave), {"insumo": c["ingrediente"].strip(), "unidad": unidad_base, "cantidad": 0.0})
            q["cantidad"] += c["cantidad"] * factor
        else:
            sin_unidad[c["ingrediente"].strip()] = sin_unidad.get(c["ingrediente"].strip(), 0) + 1

    costo = _clasificar(list(por_costo.values()), "gasto")
    for f in costo:
        f["gasto"] = round(f["gasto"], 2)

    cantidad_por_unidad = []
    for unidad in sorted({u for u, _ in por_cantidad}):
        filas = _clasificar([v for (u, _), v in por_cantidad.items() if u == unidad], "cantidad")
        for f in filas:
            f["cantidad"] = round(f["cantidad"], 3)
        cantidad_por_unidad.append({"unidad": unidad, "insumos": filas})

    resumen_clases = {}
    for clase in ("A", "B", "C"):
        filas = [f for f in costo if f["clase"] == clase]
        resumen_clases[clase] = {
            "num_insumos": len(filas),
            "gasto": round(sum(f["gasto"] for f in filas), 2),
        }

    return {
        "num_compras_reales": len(reales),
        "num_referencias_excluidas": len(todas) - len(reales),
        "num_insumos": len(costo),
        "gasto_total": round(sum(f["gasto"] for f in costo), 2),
        "resumen_clases": resumen_clases,
        "por_costo": costo,
        "por_cantidad": cantidad_por_unidad,
        "sin_unidad_reconocible": [{"insumo": k, "compras": v} for k, v in sorted(sin_unidad.items())],
    }
