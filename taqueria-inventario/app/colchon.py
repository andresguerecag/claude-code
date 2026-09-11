"""
Colchon de efectivo mensual (Fase 2b): replica el Excel FORMATO_EFECTIVO
que ya llevaban a mano.

Cada mes se registran las salidas grandes de efectivo (nomina completa,
compras al mayoreo) -- distintas de los gastos chicos del dia a dia que ya
se capturan en dinero.py -- y se calcula cuanto efectivo deberian tener en
caja:

    debo_tener = colchon_inicial_del_mes + efectivo_de_los_cortes_del_mes
                 - salidas_grandes_registradas_del_mes

El colchon inicial de cada mes se captura a mano (es el sobrante real del
mes anterior, no algo que se pueda calcular solo) -- si no se ha
capturado, "debo tener" se muestra como no disponible en vez de asumir $0.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db, historial_dinero

DB_PATH = Path(__file__).parent / "historial.db"


def _conectar_sqlite() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS salidas_efectivo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            sucursal TEXT NOT NULL,
            concepto TEXT NOT NULL,
            categoria TEXT NOT NULL,
            monto REAL NOT NULL,
            creado_en TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS colchon_inicial (
            sucursal TEXT NOT NULL,
            anio_mes TEXT NOT NULL,
            monto REAL NOT NULL,
            PRIMARY KEY (sucursal, anio_mes)
        )
        """
    )
    return con


def agregar_salida_efectivo(fecha: str, sucursal: str, concepto: str, categoria: str, monto: float) -> int:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO salidas_efectivo (fecha, sucursal, concepto, categoria, monto)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
                """,
                (fecha, sucursal, concepto, categoria, monto),
            )
            nuevo_id = cur.fetchone()[0]
        con.close()
        return nuevo_id

    con = _conectar_sqlite()
    with con:
        cur = con.execute(
            "INSERT INTO salidas_efectivo (fecha, sucursal, concepto, categoria, monto) VALUES (?, ?, ?, ?, ?)",
            (fecha, sucursal, concepto, categoria, monto),
        )
        nuevo_id = cur.lastrowid
    con.close()
    return nuevo_id


def eliminar_salida_efectivo(id_salida: int) -> None:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute("DELETE FROM salidas_efectivo WHERE id = %s", (id_salida,))
        con.close()
        return

    con = _conectar_sqlite()
    with con:
        con.execute("DELETE FROM salidas_efectivo WHERE id = ?", (id_salida,))
    con.close()


def listar_salidas_efectivo(sucursal: str, anio_mes: str) -> list[dict]:
    """anio_mes en formato 'YYYY-MM'."""
    patron = f"{anio_mes}%"
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT id, fecha, concepto, categoria, monto FROM salidas_efectivo
                WHERE sucursal = %s AND fecha LIKE %s
                ORDER BY fecha
                """,
                (sucursal, patron),
            )
            filas = cur.fetchall()
        con.close()
    else:
        con = _conectar_sqlite()
        filas = con.execute(
            "SELECT id, fecha, concepto, categoria, monto FROM salidas_efectivo "
            "WHERE sucursal = ? AND fecha LIKE ? ORDER BY fecha",
            (sucursal, patron),
        ).fetchall()
        con.close()
    return [{"id": i, "fecha": f, "concepto": c, "categoria": cat, "monto": m} for i, f, c, cat, m in filas]


def guardar_colchon_inicial(sucursal: str, anio_mes: str, monto: float) -> None:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO colchon_inicial (sucursal, anio_mes, monto) VALUES (%s, %s, %s)
                ON CONFLICT (sucursal, anio_mes) DO UPDATE SET monto = EXCLUDED.monto
                """,
                (sucursal, anio_mes, monto),
            )
        con.close()
        return

    con = _conectar_sqlite()
    with con:
        con.execute(
            """
            INSERT INTO colchon_inicial (sucursal, anio_mes, monto) VALUES (?, ?, ?)
            ON CONFLICT(sucursal, anio_mes) DO UPDATE SET monto = excluded.monto
            """,
            (sucursal, anio_mes, monto),
        )
    con.close()


def obtener_colchon_inicial(sucursal: str, anio_mes: str) -> float | None:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute(
                "SELECT monto FROM colchon_inicial WHERE sucursal = %s AND anio_mes = %s", (sucursal, anio_mes)
            )
            fila = cur.fetchone()
        con.close()
        return fila[0] if fila else None

    con = _conectar_sqlite()
    fila = con.execute(
        "SELECT monto FROM colchon_inicial WHERE sucursal = ? AND anio_mes = ?", (sucursal, anio_mes)
    ).fetchone()
    con.close()
    return fila[0] if fila else None


def calcular_debo_tener(sucursal: str, anio_mes: str) -> dict:
    colchon_inicial = obtener_colchon_inicial(sucursal, anio_mes)
    entrada_efectivo_mes = historial_dinero.total_efectivo_mes(sucursal, anio_mes)
    salidas = listar_salidas_efectivo(sucursal, anio_mes)
    salida_efectivo_mes = sum(s["monto"] for s in salidas)

    debo_tener = None
    if colchon_inicial is not None:
        debo_tener = round(colchon_inicial + entrada_efectivo_mes - salida_efectivo_mes, 2)

    return {
        "sucursal": sucursal,
        "anio_mes": anio_mes,
        "colchon_inicial": colchon_inicial,
        "entrada_efectivo_mes": round(entrada_efectivo_mes, 2),
        "salida_efectivo_mes": round(salida_efectivo_mes, 2),
        "debo_tener": debo_tener,
        "salidas": salidas,
    }
