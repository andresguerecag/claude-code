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

import json
import sqlite3
from pathlib import Path

from . import db

DB_PATH = Path(__file__).parent / "historial.db"
INICIALES_PATH = Path(__file__).parent / "compras_iniciales.json"


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
    columnas = {fila[1] for fila in con.execute("PRAGMA table_info(compras)")}
    if "sucursal" not in columnas:
        con.execute("ALTER TABLE compras ADD COLUMN sucursal TEXT")
    return con


def _compras_iniciales() -> list[dict]:
    if not INICIALES_PATH.exists():
        return []
    with open(INICIALES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _sembrar_si_vacio() -> None:
    """La primera vez que corre (base de datos vacia, ya sea local o recien
    hosteada), se siembra con el catalogo real de precios que compartio la
    familia (ver compras_iniciales.json) -- no arranca de cero. Despues de
    eso, esta funcion ya no hace nada (la tabla deja de estar vacia)."""
    iniciales = _compras_iniciales()
    if not iniciales:
        return

    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM compras")
            if cur.fetchone()[0] == 0:
                for c in iniciales:
                    cur.execute(
                        """
                        INSERT INTO compras (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (c["fecha"], c["ingrediente"], c["proveedor"], c.get("cantidad"), c.get("unidad"),
                         c["precio_total"], c.get("notas", ""), c.get("sucursal")),
                    )
        con.close()
        return

    con = _conectar_sqlite()
    with con:
        count = con.execute("SELECT COUNT(*) FROM compras").fetchone()[0]
        if count == 0:
            for c in iniciales:
                con.execute(
                    """
                    INSERT INTO compras (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (c["fecha"], c["ingrediente"], c["proveedor"], c.get("cantidad"), c.get("unidad"),
                     c["precio_total"], c.get("notas", ""), c.get("sucursal")),
                )
    con.close()


def _normalizar(texto: str) -> str:
    return str(texto).strip().upper()


def agregar_compra(
    fecha: str, ingrediente: str, proveedor: str, cantidad: float | None,
    unidad: str | None, precio_total: float, notas: str = "", sucursal: str | None = None,
) -> int:
    ingrediente = ingrediente.strip()
    proveedor = proveedor.strip()
    if not ingrediente:
        raise ValueError("Falta el ingrediente.")
    if not proveedor:
        raise ValueError("Falta el proveedor.")
    sucursal = sucursal.strip() if sucursal else None

    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con, con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO compras (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal),
            )
            nuevo_id = cur.fetchone()[0]
        con.close()
        return nuevo_id

    con = _conectar_sqlite()
    with con:
        cur = con.execute(
            """
            INSERT INTO compras (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal),
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


#
# Distintas compras del mismo ingrediente a veces vienen en unidades
# distintas pero equivalentes (ej. aceite por mililitro una vez y por litro
# otra) -- si se promedian tal cual, el resultado no significa nada. Aqui se
# normalizan las unidades conocidas (peso, volumen, pieza) a una unidad base
# por familia para poder comparar de verdad. Una unidad que no se reconoce
# (ej. "paq.", "caja", texto de bulto) se deja tal cual y NUNCA se mezcla
# con otra familia -- mejor no comparar que comparar mal.
_UNIDADES_EQUIVALENTES = {
    "kg": ("kg", 1.0), "kilo": ("kg", 1.0), "kilos": ("kg", 1.0), "kilogramo": ("kg", 1.0), "kilogramos": ("kg", 1.0),
    "g": ("kg", 0.001), "gr": ("kg", 0.001), "grs": ("kg", 0.001), "gramo": ("kg", 0.001), "gramos": ("kg", 0.001),
    "l": ("l", 1.0), "lt": ("l", 1.0), "litro": ("l", 1.0), "litros": ("l", 1.0),
    "ml": ("l", 0.001), "mililitro": ("l", 0.001), "mililitros": ("l", 0.001),
    "pza": ("pza", 1.0), "pzas": ("pza", 1.0), "pieza": ("pza", 1.0), "piezas": ("pza", 1.0), "unidad": ("pza", 1.0),
}


def _familia_unidad(unidad: str | None) -> tuple[str, float] | None:
    """(unidad_base, factor_a_unidad_base) para unidades conocidas, o None
    si la unidad es libre/no reconocida con confianza."""
    if not unidad:
        return None
    return _UNIDADES_EQUIVALENTES.get(str(unidad).strip().lower())


def _fila_a_dict(fila) -> dict:
    id_, fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal = fila
    precio_unitario = round(precio_total / cantidad, 4) if cantidad else None
    unidad_base = precio_normalizado = None
    familia = _familia_unidad(unidad)
    if precio_unitario is not None and familia:
        unidad_base, factor = familia
        precio_normalizado = round(precio_unitario / factor, 2)
    return {
        "id": id_, "fecha": fecha, "ingrediente": ingrediente, "proveedor": proveedor,
        "cantidad": cantidad, "unidad": unidad, "precio_total": precio_total,
        "precio_unitario": round(precio_unitario, 2) if precio_unitario is not None else None,
        "unidad_base": unidad_base, "precio_normalizado": precio_normalizado,
        "notas": notas, "sucursal": sucursal,
    }


def listar_compras(
    ingrediente: str | None = None, proveedor: str | None = None,
    desde: str | None = None, hasta: str | None = None, sucursal: str | None = None,
) -> list[dict]:
    """Todas las compras, mas recientes primero. 'ingrediente'/'proveedor'/
    'sucursal' filtran por coincidencia parcial (insensible a mayusculas)."""
    _sembrar_si_vacio()
    if db.usando_postgres():
        db.inicializar_tablas()
        con = db.conectar()
        with con.cursor() as cur:
            cur.execute(
                "SELECT id, fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal "
                "FROM compras ORDER BY fecha DESC, id DESC"
            )
            filas = cur.fetchall()
        con.close()
    else:
        con = _conectar_sqlite()
        filas = con.execute(
            "SELECT id, fecha, ingrediente, proveedor, cantidad, unidad, precio_total, notas, sucursal "
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
    if sucursal:
        obj = _normalizar(sucursal)
        resultado = [c for c in resultado if c["sucursal"] and obj in _normalizar(c["sucursal"])]
    if desde:
        resultado = [c for c in resultado if c["fecha"] >= desde]
    if hasta:
        resultado = [c for c in resultado if c["fecha"] <= hasta]
    return resultado


def historial_ingrediente(ingrediente: str, limite: int = 20) -> list[dict]:
    """Compras pasadas de un ingrediente (coincidencia parcial), para
    comparar contra un precio nuevo -- usado por el asesor de compras."""
    return listar_compras(ingrediente=ingrediente)[:limite]


def sugerir_por_proveedor(proveedor: str) -> list[dict]:
    """"Voy a comprar a X" -- de todos los ingredientes que alguna vez se
    han comprado en ese proveedor, dice cual es el ultimo precio visto ahi
    y como se compara contra el promedio de ese mismo ingrediente en TODOS
    los proveedores (en la misma unidad base), para saber cuales aprovechar
    ahi y cuales conviene comprar en otro lado."""
    todas = listar_compras()
    obj = _normalizar(proveedor)
    del_proveedor = [c for c in todas if obj in _normalizar(c["proveedor"])]
    if not del_proveedor:
        return []

    ultimo_por_ingrediente: dict[str, dict] = {}
    for c in del_proveedor:  # listar_compras ya viene mas reciente primero
        clave = _normalizar(c["ingrediente"])
        if clave not in ultimo_por_ingrediente:
            ultimo_por_ingrediente[clave] = c

    resultado = []
    for clave, compra_aqui in ultimo_por_ingrediente.items():
        comparables = [
            c for c in todas
            if _normalizar(c["ingrediente"]) == clave
            and c["unidad_base"] is not None
            and c["unidad_base"] == compra_aqui["unidad_base"]
        ]
        precios = [c["precio_normalizado"] for c in comparables]
        promedio = round(sum(precios) / len(precios), 2) if precios else None
        minimo = round(min(precios), 2) if precios else None
        precio_aqui = compra_aqui["precio_normalizado"]
        resultado.append({
            "ingrediente": compra_aqui["ingrediente"],
            "ultima_fecha_aqui": compra_aqui["fecha"],
            "precio_normalizado_aqui": precio_aqui,
            "unidad_base": compra_aqui["unidad_base"],
            "precio_promedio_general": promedio,
            "precio_minimo_general": minimo,
            "num_comparables": len(precios),
            "buena_compra_aqui": (
                round(precio_aqui, 2) <= promedio if precio_aqui is not None and promedio is not None else None
            ),
        })
    resultado.sort(key=lambda x: x["ingrediente"])
    return resultado


def comparar_precio(ingrediente: str, cantidad: float | None, unidad: str | None, precio_total: float) -> dict:
    """Compara un precio nuevo contra el historial de ese ingrediente.

    Solo promedia contra compras que esten en la MISMA unidad base (peso,
    volumen o pieza) -- nunca mezcla, por ejemplo, un precio por mililitro
    contra uno por litro. Si la unidad del precio nuevo no se reconoce, o no
    hay historial en esa misma unidad, lo dice explicitamente en vez de dar
    un promedio que mezclaria unidades distintas."""
    historial = historial_ingrediente(ingrediente, limite=1000)
    precio_unitario_visto = round(precio_total / cantidad, 2) if cantidad else round(precio_total, 2)

    familia = _familia_unidad(unidad)
    unidad_base = precio_normalizado_visto = None
    comparables = []
    if familia:
        unidad_base, factor = familia
        precio_normalizado_visto = round(precio_unitario_visto / factor, 2) if cantidad else None
        comparables = [c for c in historial if c["unidad_base"] == unidad_base and c["precio_normalizado"] is not None]

    precios = [c["precio_normalizado"] for c in comparables]
    return {
        "precio_unitario_visto": precio_unitario_visto,
        "unidad_base": unidad_base,
        "precio_normalizado_visto": precio_normalizado_visto,
        "num_compras": len(historial),
        "num_comparables": len(comparables),
        "precio_promedio": round(sum(precios) / len(precios), 2) if precios else None,
        "precio_minimo": round(min(precios), 2) if precios else None,
        "precio_maximo": round(max(precios), 2) if precios else None,
        "historial": historial,
    }
