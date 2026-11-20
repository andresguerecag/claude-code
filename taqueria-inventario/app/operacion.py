"""
Operación diaria de la Versión 2, sin Excel:

- Inventario del día por sucursal: la encargada captura inicial, lo que
  entró (compras) y el conteo final de cada producto. El inicial se propone
  solo con el final del día anterior de esa sucursal (se puede corregir).
- Comparativo automático: lo cobrado en la caja contra lo que se gastó del
  inventario, con las mismas recetas y tolerancias que la Versión 1.
- Resumen del día: el semáforo que revisa la administración, por sucursal:
  ¿cuadró el efectivo? ¿hubo merma fuera de tolerancia?

Los productos son los que alimentan el comparativo (los mismos nombres del
formato de corte), para no pedir contar cosas que no se usan.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import caja, db, reconciliacion

DB_PATH = Path(__file__).parent / "historial.db"
TOLERANCIA_EFECTIVO = 20.0  # la misma que usa el cuadre de caja de la Versión 1

# (producto, unidad, para qué sirve) -- en el orden en que se cuentan.
PRODUCTOS = [
    ("ARRACHERA COCIDA", "kg", "carne"),
    ("BISTEK COCIDO", "kg", "carne"),
    ("TROMPO COCIDO", "kg", "pastor"),
    ("QUESO CHIHUAHUA", "kg", "queso"),
    ("TORTILLA", "kg", "tortillas"),
    ("TELERA", "pza", "telera"),
    ("REFRESCO 600", "pza", "refrescos"),
    ("AGUA CIEL", "pza", "refrescos"),
]

_DDL = """CREATE TABLE IF NOT EXISTS conteos_inventario (
    fecha TEXT NOT NULL,
    sucursal TEXT NOT NULL,
    producto TEXT NOT NULL,
    inicial {real},
    entrada {real},
    final {real},
    PRIMARY KEY (fecha, sucursal, producto)
)"""


def _conectar():
    if db.usando_postgres():
        con = db.conectar()
        real = "DOUBLE PRECISION"
    else:
        con = sqlite3.connect(DB_PATH)
        real = "REAL"
    cur = con.cursor()
    cur.execute(_DDL.format(real=real))
    con.commit()
    return con


def _sql(con, consulta: str) -> str:
    return consulta if isinstance(con, sqlite3.Connection) else consulta.replace("?", "%s")


def _consultar(consulta: str, params: tuple = ()) -> list[tuple]:
    con = _conectar()
    cur = con.cursor()
    cur.execute(_sql(con, consulta), params)
    filas = cur.fetchall()
    con.close()
    return filas


def _dia_anterior(fecha: str) -> str:
    return (date.fromisoformat(fecha) - timedelta(days=1)).isoformat()


# --- inventario del dia -----------------------------------------------------------

def conteo_del_dia(fecha: str, sucursal: str) -> dict:
    guardado = {p: (i, e, f) for p, i, e, f in _consultar(
        "SELECT producto, inicial, entrada, final FROM conteos_inventario WHERE fecha = ? AND sucursal = ?", (fecha, sucursal))}
    ayer = {p: f for p, f in _consultar(
        "SELECT producto, final FROM conteos_inventario WHERE fecha = ? AND sucursal = ?", (_dia_anterior(fecha), sucursal))}
    filas = []
    for producto, unidad, insumo in PRODUCTOS:
        inicial, entrada, final = guardado.get(producto, (None, None, None))
        sugerido = inicial is None and ayer.get(producto) is not None
        if sugerido:
            inicial = ayer[producto]
        consumo = round(inicial + (entrada or 0) - final, 3) if inicial is not None and final is not None else None
        filas.append({
            "producto": producto, "unidad": unidad, "insumo": insumo,
            "inicial": inicial, "inicial_sugerido": sugerido, "entrada": entrada, "final": final, "consumo": consumo,
        })
    return {"fecha": fecha, "sucursal": sucursal, "capturado": bool(guardado), "productos": filas}


def guardar_conteo(fecha: str, sucursal: str, productos: list[dict]) -> None:
    conocidos = {p for p, _, _ in PRODUCTOS}
    con = _conectar()
    cur = con.cursor()
    for p in productos:
        nombre = str(p.get("producto", ""))
        if nombre not in conocidos:
            continue
        valores = []
        for campo in ("inicial", "entrada", "final"):
            v = p.get(campo)
            if v in (None, ""):
                valores.append(None)
                continue
            v = float(v)
            if v < 0:
                raise ValueError(f"{nombre}: el {campo} no puede ser negativo.")
            valores.append(v)
        cur.execute(_sql(con, "DELETE FROM conteos_inventario WHERE fecha = ? AND sucursal = ? AND producto = ?"), (fecha, sucursal, nombre))
        cur.execute(_sql(con, "INSERT INTO conteos_inventario (fecha, sucursal, producto, inicial, entrada, final) VALUES (?, ?, ?, ?, ?, ?)"),
                    (fecha, sucursal, nombre, *valores))
    con.commit()
    con.close()


def comparativo_del_dia(fecha: str, sucursal: str) -> dict | None:
    """Lo cobrado en la caja vs. lo gastado según el conteo. None si todavía
    no hay conteo o no hay ventas ese día."""
    conteo = conteo_del_dia(fecha, sucursal)
    if not conteo["capturado"]:
        return None
    ventas = caja.ventas_para_inventario(fecha, sucursal)
    if not ventas:
        return None
    inventario = [
        reconciliacion.ItemInventario(producto=p["producto"], unidad=p["unidad"], inicial=p["inicial"],
                                      entrada=p["entrada"] or 0.0, final=p["final"])
        for p in conteo["productos"]
    ]
    return reconciliacion.generar_reporte(None, None, ventas=ventas, inventario=inventario)


# --- resumen del dia (semaforo) ------------------------------------------------------

def _sucursales_del_dia(fecha: str) -> list[str]:
    de_conteos = [s for (s,) in _consultar("SELECT DISTINCT sucursal FROM conteos_inventario WHERE fecha = ?", (fecha,))]
    de_ventas = {v["sucursal"] for v in caja.listar_ventas(fecha)}
    return sorted(set(de_conteos) | de_ventas | set(caja.sucursales()))


def resumen_del_dia(fecha: str) -> dict:
    sucursales = []
    for suc in _sucursales_del_dia(fecha):
        corte = caja.corte_del_dia(fecha, suc)
        e = corte["efectivo"]
        if corte["num_ventas"] == 0:
            caja_estado, caja_texto = "sin_datos", "Sin ventas en la caja"
        elif e["diferencia"] is None:
            caja_estado, caja_texto = "pendiente", "Falta contar el efectivo"
        elif abs(e["diferencia"]) <= TOLERANCIA_EFECTIVO:
            caja_estado, caja_texto = "ok", "El efectivo cuadra" if abs(e["diferencia"]) < 0.01 else f"Cuadra (diferencia de ${e['diferencia']:,.2f})"
        else:
            caja_estado = "alerta"
            caja_texto = f"{'Faltan' if e['diferencia'] < 0 else 'Sobran'} ${abs(e['diferencia']):,.2f} de efectivo"

        comp = comparativo_del_dia(fecha, suc)
        conteo = conteo_del_dia(fecha, suc)
        alertas = []
        if comp is None:
            inv_estado = "pendiente" if corte["num_ventas"] else "sin_datos"
            inv_texto = "Falta capturar el inventario del día" if not conteo["capturado"] else "Sin ventas para comparar"
            if not corte["num_ventas"] and not conteo["capturado"]:
                inv_texto = "Sin datos"
        else:
            alertas = [f for f in comp["comparativo"] if f.get("alerta") is True]
            incompletos = [f for f in comp["comparativo"] if f.get("consumo_real") == "no disponible"]
            if alertas:
                inv_estado = "alerta"
                inv_texto = "Merma fuera de tolerancia en " + ", ".join(f["insumo"] for f in alertas)
            elif incompletos:
                inv_estado = "pendiente"
                inv_texto = "Conteo incompleto: " + ", ".join(f["insumo"] for f in incompletos)
            else:
                inv_estado, inv_texto = "ok", "Todo dentro de tolerancia"

        estados = {caja_estado, inv_estado}
        general = "alerta" if "alerta" in estados else "pendiente" if "pendiente" in estados else "ok" if "ok" in estados else "sin_datos"
        sucursales.append({
            "sucursal": suc, "general": general,
            "total_vendido": corte["total_vendido"], "num_ventas": corte["num_ventas"],
            "por_forma_pago": [f for f in corte["por_forma_pago"] if f["total"]],
            "caja": {"estado": caja_estado, "texto": caja_texto, **e},
            "inventario": {"estado": inv_estado, "texto": inv_texto,
                           "comparativo": comp["comparativo"] if comp else [],
                           "pendientes": comp["platillos_no_identificados"] if comp else []},
        })
    return {"fecha": fecha, "sucursales": sucursales}
