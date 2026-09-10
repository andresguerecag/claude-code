"""
Historial de reportes ya generados.

Usa Postgres (Supabase) cuando la app esta hosteada (DATABASE_URL
configurada), para que sobreviva a que el hosting gratis borre el disco en
cada redeploy. Si no hay DATABASE_URL (al correr local en tu compu para
probar), usa un archivo SQLite local -- no necesitas internet para probar.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import db

DB_PATH = Path(__file__).parent / "historial.db"


def _conectar_sqlite() -> sqlite3.Connection:
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
    reporte_json = json.dumps(reporte, ensure_ascii=False)
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reportes (fecha, sucursal, reporte_json, creado_en)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (fecha, sucursal) DO UPDATE SET
                    reporte_json = EXCLUDED.reporte_json,
                    creado_en = EXCLUDED.creado_en
                """,
                (fecha, sucursal, reporte_json),
            )
        con.close()
        return

    con = _conectar_sqlite()
    with con:
        con.execute(
            """
            INSERT INTO reportes (fecha, sucursal, reporte_json, creado_en)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(fecha, sucursal) DO UPDATE SET
                reporte_json = excluded.reporte_json,
                creado_en = excluded.creado_en
            """,
            (fecha, sucursal, reporte_json),
        )
    con.close()


def _todos_los_reportes() -> list[tuple[str, str, str, str]]:
    """(fecha, sucursal, reporte_json, creado_en) de todos los dias."""
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute("SELECT fecha, sucursal, reporte_json, creado_en::text FROM reportes ORDER BY fecha DESC, sucursal ASC")
            filas = cur.fetchall()
        con.close()
        return filas

    con = _conectar_sqlite()
    filas = con.execute(
        "SELECT fecha, sucursal, reporte_json, creado_en FROM reportes ORDER BY fecha DESC, sucursal ASC"
    ).fetchall()
    con.close()
    return filas


def listar_historial() -> list[dict]:
    resumen = []
    for fecha, sucursal, reporte_json, creado_en in _todos_los_reportes():
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
    acumulado: dict[str, dict] = {}
    for _fecha, _sucursal, reporte_json, _creado_en in _todos_los_reportes():
        r = json.loads(reporte_json)
        for p in r.get("platillos_no_identificados", []):
            clave = p["clave"]
            if clave not in acumulado:
                acumulado[clave] = {"clave": clave, "nombre": p["nombre"], "cantidad_total": 0, "dias": 0}
            acumulado[clave]["cantidad_total"] += p["cantidad"]
            acumulado[clave]["dias"] += 1
    return sorted(acumulado.values(), key=lambda x: x["cantidad_total"], reverse=True)


def dashboard(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None) -> dict:
    """
    Resumen filtrable para el dashboard: ventas por dia (por sucursal),
    alertas, y consumo acumulado por insumo en el periodo. Todo calculado
    sobre lo que ya esta en el historial -- no vuelve a leer excels.
    """
    sucursales_vistas: set[str] = set()
    dias = []
    merma_acum: dict[str, dict] = {}
    total_vendido = 0.0
    total_alertas = 0
    suma_pct = 0.0
    num_reportes_pct = 0

    for fecha, suc, reporte_json, _creado_en in _todos_los_reportes():
        sucursales_vistas.add(suc)
        if sucursal and sucursal != "todas" and suc != sucursal:
            continue
        if desde and fecha < desde:
            continue
        if hasta and fecha > hasta:
            continue

        r = json.loads(reporte_json)
        num_alertas = sum(1 for f in r.get("comparativo", []) if f.get("alerta") is True)
        dias.append({
            "fecha": fecha,
            "sucursal": suc,
            "total_platillos_vendidos": r.get("total_platillos_vendidos", 0),
            "num_alertas": num_alertas,
            "pct_identificado": r.get("pct_identificado"),
        })
        total_vendido += r.get("total_platillos_vendidos", 0) or 0
        total_alertas += num_alertas
        if r.get("pct_identificado") is not None:
            suma_pct += r["pct_identificado"]
            num_reportes_pct += 1

        for f in r.get("comparativo", []):
            insumo = f["insumo"]
            if insumo not in merma_acum:
                merma_acum[insumo] = {"insumo": insumo, "consumo_real": 0.0, "consumo_teorico": 0.0, "con_dato": False}
            if isinstance(f.get("consumo_real"), (int, float)) and isinstance(f.get("consumo_teorico"), (int, float)):
                merma_acum[insumo]["consumo_real"] += f["consumo_real"]
                merma_acum[insumo]["consumo_teorico"] += f["consumo_teorico"]
                merma_acum[insumo]["con_dato"] = True

    dias.sort(key=lambda d: (d["fecha"], d["sucursal"]))

    merma_lista = []
    for insumo, v in merma_acum.items():
        if not v["con_dato"]:
            merma_lista.append({"insumo": insumo, "consumo_real": "no disponible", "consumo_teorico": "no disponible", "diferencia": "no disponible"})
        else:
            merma_lista.append({
                "insumo": insumo,
                "consumo_real": round(v["consumo_real"], 2),
                "consumo_teorico": round(v["consumo_teorico"], 2),
                "diferencia": round(v["consumo_teorico"] - v["consumo_real"], 2),
            })

    return {
        "sucursales": sorted(sucursales_vistas),
        "dias": dias,
        "merma_por_insumo": merma_lista,
        "resumen": {
            "total_platillos_vendidos": total_vendido,
            "total_alertas": total_alertas,
            "num_dias": len(dias),
            "pct_identificado_promedio": round(suma_pct / num_reportes_pct, 1) if num_reportes_pct else None,
        },
    }


def obtener_reporte(fecha: str, sucursal: str) -> dict | None:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute("SELECT reporte_json FROM reportes WHERE fecha = %s AND sucursal = %s", (fecha, sucursal))
            fila = cur.fetchone()
        con.close()
        return json.loads(fila[0]) if fila else None

    con = _conectar_sqlite()
    fila = con.execute(
        "SELECT reporte_json FROM reportes WHERE fecha = ? AND sucursal = ?", (fecha, sucursal)
    ).fetchone()
    con.close()
    return json.loads(fila[0]) if fila else None
