"""
Historial de reportes ya generados, guardado en un archivo de base de datos
local (SQLite -- no requiere instalar nada aparte ni hostear nada, es un
archivo mas dentro de la carpeta de la app, como un Excel).

Si en algun momento varias personas en distintas computadoras necesitan ver
el mismo historial al mismo tiempo, esto es lo que habria que mover a un
servidor con hosting -- mientras tanto, vive tranquilamente en local.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "historial.db"


def _conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reportes (
            fecha TEXT NOT NULL,
            sucursal TEXT NOT NULL,
            reporte_json TEXT NOT NULL,
            creado_en TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (fecha, sucursal)
        )
        """
    )
    return con


def guardar_reporte(fecha: str, sucursal: str, reporte: dict) -> None:
    """Guarda (o reemplaza, si ya existia) el reporte de ese dia+sucursal."""
    con = _conectar()
    with con:
        con.execute(
            """
            INSERT INTO reportes (fecha, sucursal, reporte_json, creado_en)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(fecha, sucursal) DO UPDATE SET
                reporte_json = excluded.reporte_json,
                creado_en = excluded.creado_en
            """,
            (fecha, sucursal, json.dumps(reporte, ensure_ascii=False)),
        )
    con.close()


def listar_historial() -> list[dict]:
    """Resumen de todos los dias guardados, mas reciente primero."""
    con = _conectar()
    filas = con.execute(
        "SELECT fecha, sucursal, reporte_json, creado_en FROM reportes ORDER BY fecha DESC, sucursal ASC"
    ).fetchall()
    con.close()

    resumen = []
    for fecha, sucursal, reporte_json, creado_en in filas:
        r = json.loads(reporte_json)
        num_alertas = sum(1 for f in r.get("comparativo", []) if f.get("alerta") is True)
        resumen.append({
            "fecha": fecha,
            "sucursal": sucursal,
            "pct_identificado": r.get("pct_identificado"),
            "total_platillos_vendidos": r.get("total_platillos_vendidos"),
            "num_alertas": num_alertas,
            "guardado_en": creado_en,
        })
    return resumen


def pendientes_acumulados() -> list[dict]:
    """
    Junta los platillos no identificados de TODOS los dias guardados, sumando
    cuanto se ha vendido de cada uno en total. Pensado para una sesion de
    'vamos a afinar las recetas de una vez' -- prioriza por volumen en vez
    de ir dia por dia.
    """
    con = _conectar()
    filas = con.execute("SELECT reporte_json FROM reportes").fetchall()
    con.close()

    acumulado: dict[str, dict] = {}
    for (reporte_json,) in filas:
        r = json.loads(reporte_json)
        for p in r.get("platillos_no_identificados", []):
            clave = p["clave"]
            if clave not in acumulado:
                acumulado[clave] = {"clave": clave, "nombre": p["nombre"], "cantidad_total": 0, "dias": 0}
            acumulado[clave]["cantidad_total"] += p["cantidad"]
            acumulado[clave]["dias"] += 1

    return sorted(acumulado.values(), key=lambda x: x["cantidad_total"], reverse=True)


def obtener_reporte(fecha: str, sucursal: str) -> dict | None:
    con = _conectar()
    fila = con.execute(
        "SELECT reporte_json FROM reportes WHERE fecha = ? AND sucursal = ?",
        (fecha, sucursal),
    ).fetchone()
    con.close()
    if fila is None:
        return None
    return json.loads(fila[0])
