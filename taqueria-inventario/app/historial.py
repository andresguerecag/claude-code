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
