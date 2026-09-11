"""
Historial de reportes de dinero (Fase 2) ya generados.

Misma logica dual Postgres/SQLite que historial.py -- usa Postgres cuando
la app esta hosteada (DATABASE_URL configurada) y SQLite local para
probar en tu compu.
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
        CREATE TABLE IF NOT EXISTS reportes_dinero (
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
                INSERT INTO reportes_dinero (fecha, sucursal, reporte_json, creado_en)
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
            INSERT INTO reportes_dinero (fecha, sucursal, reporte_json, creado_en)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(fecha, sucursal) DO UPDATE SET
                reporte_json = excluded.reporte_json,
                creado_en = excluded.creado_en
            """,
            (fecha, sucursal, reporte_json),
        )
    con.close()


def _todos_los_reportes() -> list[tuple[str, str, str, str]]:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute("SELECT fecha, sucursal, reporte_json, creado_en::text FROM reportes_dinero ORDER BY fecha DESC, sucursal ASC")
            filas = cur.fetchall()
        con.close()
        return filas

    con = _conectar_sqlite()
    filas = con.execute(
        "SELECT fecha, sucursal, reporte_json, creado_en FROM reportes_dinero ORDER BY fecha DESC, sucursal ASC"
    ).fetchall()
    con.close()
    return filas


def listar_historial(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None) -> list[dict]:
    resumen = []
    for fecha, suc, reporte_json, creado_en in _todos_los_reportes():
        if sucursal and sucursal != "todas" and suc != sucursal:
            continue
        if desde and fecha < desde:
            continue
        if hasta and fecha > hasta:
            continue
        r = json.loads(reporte_json)
        resumen.append({
            "fecha": fecha,
            "sucursal": suc,
            "sobra_falta": r.get("sobra_falta"),
            "alerta_caja": r.get("alerta_caja"),
            "total_gastos": r.get("total_gastos"),
            "pct_categorizado": r.get("pct_categorizado"),
            "guardado_en": creado_en,
        })
    return resumen


def eliminar_reporte(fecha: str, sucursal: str) -> None:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute("DELETE FROM reportes_dinero WHERE fecha = %s AND sucursal = %s", (fecha, sucursal))
        con.close()
        return

    con = _conectar_sqlite()
    with con:
        con.execute("DELETE FROM reportes_dinero WHERE fecha = ? AND sucursal = ?", (fecha, sucursal))
    con.close()


def obtener_reporte(fecha: str, sucursal: str) -> dict | None:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute("SELECT reporte_json FROM reportes_dinero WHERE fecha = %s AND sucursal = %s", (fecha, sucursal))
            fila = cur.fetchone()
        con.close()
        return json.loads(fila[0]) if fila else None

    con = _conectar_sqlite()
    fila = con.execute(
        "SELECT reporte_json FROM reportes_dinero WHERE fecha = ? AND sucursal = ?", (fecha, sucursal)
    ).fetchone()
    con.close()
    return json.loads(fila[0]) if fila else None


def total_efectivo_mes(sucursal: str, anio_mes: str) -> float:
    """Suma el efectivo capturado en los cortes de ese mes (anio_mes en
    formato 'YYYY-MM') -- lo usa colchon.py para calcular 'debo tener'."""
    total = 0.0
    for fecha, suc, reporte_json, _creado_en in _todos_los_reportes():
        if suc != sucursal or not fecha.startswith(anio_mes):
            continue
        r = json.loads(reporte_json)
        efvo = (r.get("ingresos") or {}).get("efectivo")
        if isinstance(efvo, (int, float)):
            total += efvo
    return total


def pendientes_acumulados() -> list[dict]:
    """Conceptos de gasto sin categorizar de todo el historial, sumados y
    ordenados por monto -- para priorizar una sesion de categorizar."""
    acumulado: dict[str, dict] = {}
    for _fecha, _sucursal, reporte_json, _creado_en in _todos_los_reportes():
        r = json.loads(reporte_json)
        for g in r.get("gastos_sin_categorizar", []):
            concepto = g["concepto"]
            if concepto not in acumulado:
                acumulado[concepto] = {"concepto": concepto, "monto_total": 0.0, "dias": 0}
            acumulado[concepto]["monto_total"] += g["monto"]
            acumulado[concepto]["dias"] += 1
    return sorted(acumulado.values(), key=lambda x: x["monto_total"], reverse=True)


def dashboard(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None) -> dict:
    """Resumen filtrable: sobra/falta por dia, gastos por categoria
    acumulados, y alertas de caja -- todo calculado sobre lo que ya esta
    en el historial."""
    sucursales_vistas: set[str] = set()
    dias = []
    gastos_acum: dict[str, float] = {}
    total_gastos = 0.0
    total_ingresos_efectivo = 0.0
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
        dias.append({
            "fecha": fecha,
            "sucursal": suc,
            "sobra_falta": r.get("sobra_falta"),
            "alerta_caja": r.get("alerta_caja"),
            "total_gastos": r.get("total_gastos"),
            "pct_categorizado": r.get("pct_categorizado"),
        })
        if r.get("alerta_caja"):
            total_alertas += 1
        total_gastos += r.get("total_gastos") or 0
        efvo = (r.get("ingresos") or {}).get("efectivo")
        if isinstance(efvo, (int, float)):
            total_ingresos_efectivo += efvo
        if r.get("pct_categorizado") is not None:
            suma_pct += r["pct_categorizado"]
            num_reportes_pct += 1

        for cat in r.get("gastos_por_categoria", []):
            gastos_acum[cat["categoria"]] = gastos_acum.get(cat["categoria"], 0.0) + cat["monto"]

    dias.sort(key=lambda d: (d["fecha"], d["sucursal"]))

    return {
        "sucursales": sorted(sucursales_vistas),
        "dias": dias,
        "gastos_por_categoria": [
            {"categoria": cat, "monto": round(monto, 2)}
            for cat, monto in sorted(gastos_acum.items(), key=lambda x: x[1], reverse=True)
        ],
        "resumen": {
            "total_gastos": round(total_gastos, 2),
            "total_ingresos_efectivo": round(total_ingresos_efectivo, 2),
            "total_alertas_caja": total_alertas,
            "num_dias": len(dias),
            "pct_categorizado_promedio": round(suma_pct / num_reportes_pct, 1) if num_reportes_pct else None,
        },
    }
