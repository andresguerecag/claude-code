"""
Base de datos de compras: que ingrediente se compro, en donde (proveedor) y
a que precio.

Sirve para dos cosas:
  1. Llevar un historial de precios por ingrediente/proveedor a lo largo
     del tiempo (ej. "cuanto ha costado el aceite en Sam's").
  2. Alimentar al asesor de compras (ver asesor_compras.py) para poder
     responder "a este precio, ¿conviene comprarlo?" comparando contra
     compras anteriores -- nunca inventando un precio de referencia.

Misma logica dual Postgres/SQLite que el resto de la app.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db

DB_PATH = Path(__file__).parent / "historial.db"


def _conectar_sqlite() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS compras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            ingrediente TEXT NOT NULL,
            proveedor TEXT NOT NULL,
            cantidad REAL,
            unidad TEXT,
            precio_total REAL NOT NULL,
            notas TEXT,
            creado_en TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return con


def _normalizar(texto: str) -> str:
    return str(texto).strip().upper()


def agregar_compra(
    fecha: str, ingrediente: str, proveedor: str, cantidad: float | None,
    unidad: str | None, precio_total: float, notas: str = "",
) -> int:
    ingrediente = ingrediente.strip()
    proveedor = proveedor.strip()
    if not ingrediente:
        raise ValueError("Falta el ingrediente.")
    if not proveedor:
        raise ValueError("Falta el proveedor.")

    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO compras (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas),
            )
            nuevo_id = cur.fetchone()[0]
        con.close()
        return nuevo_id

    con = _conectar_sqlite()
    with con:
        cur = con.execute(
            """
            INSERT INTO compras (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas),
        )
        nuevo_id = cur.lastrowid
    con.close()
    return nuevo_id


def eliminar_compra(id_compra: int) -> None:
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute("DELETE FROM compras WHERE id = %s", (id_compra,))
        con.close()
        return

    con = _conectar_sqlite()
    with con:
        con.execute("DELETE FROM compras WHERE id = ?", (id_compra,))
    con.close()


def _fila_a_dict(fila) -> dict:
    id_, fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas = fila
    precio_unitario = round(precio_total / cantidad, 2) if cantidad else None
    return {
        "id": id_, "fecha": fecha, "ingrediente": ingrediente, "proveedor": proveedor,
        "cantidad": cantidad, "unidad": unidad, "precio_total": precio_total,
        "precio_unitario": precio_unitario, "notas": notas,
    }


def listar_compras(
    ingrediente: str | None = None, proveedor: str | None = None,
    desde: str | None = None, hasta: str | None = None,
) -> list[dict]:
    """Todas las compras, mas recientes primero. 'ingrediente'/'proveedor'
    filtran por coincidencia parcial (insensible a mayusculas)."""
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute(
                "SELECT id, fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas "
                "FROM compras ORDER BY fecha DESC, id DESC"
            )
            filas = cur.fetchall()
        con.close()
    else:
        con = _conectar_sqlite()
        filas = con.execute(
            "SELECT id, fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas "
            "FROM compras ORDER BY fecha DESC, id DESC"
        ).fetchall()
        con.close()

    resultado = [_fila_a_dict(f) for f in filas]
    if ingrediente:
        obj = _normalizar(ingrediente)
        resultado = [c for c in resultado if obj in _normalizar(c["ingrediente"])]
    if proveedor:
        obj = _normalizar(proveedor)
        resultado = [c for c in resultado if obj in _normalizar(c["proveedor"])]
    if desde:
        resultado = [c for c in resultado if c["fecha"] >= desde]
    if hasta:
        resultado = [c for c in resultado if c["fecha"] <= hasta]
    return resultado


def historial_ingrediente(ingrediente: str, limite: int = 20) -> list[dict]:
    """Compras pasadas de un ingrediente (coincidencia parcial), para
    comparar contra un precio nuevo -- usado por el asesor de compras."""
    return listar_compras(ingrediente=ingrediente)[:limite]


def estadisticas_ingrediente(ingrediente: str) -> dict:
    """Precio unitario promedio/minimo/maximo historico de un ingrediente,
    calculado solo con compras que tienen cantidad (para poder sacar
    precio unitario) -- nunca se inventa un precio si no hay historial."""
    compras_previas = historial_ingrediente(ingrediente, limite=1000)
    precios = [c["precio_unitario"] for c in compras_previas if c["precio_unitario"] is not None]
    if not precios:
        return {"num_compras": len(compras_previas), "precio_promedio": None, "precio_minimo": None, "precio_maximo": None}
    return {
        "num_compras": len(compras_previas),
        "precio_promedio": round(sum(precios) / len(precios), 2),
        "precio_minimo": round(min(precios), 2),
        "precio_maximo": round(max(precios), 2),
    }
