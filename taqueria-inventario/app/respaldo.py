"""
Respaldo completo de la base de datos en un Excel, pedido por el papa: que
todo quede guardado fuera del servidor por si algun dia Supabase (o el
hosting) truena.

Exporta TODAS las tablas tal cual estan (una hoja por tabla), sin elegir
cuales: si mañana se agrega una tabla nueva, entra sola al respaldo. Corriendo
local (sin Postgres), ademas de las tablas de SQLite incluye los JSON donde
viven recetas, mapeos y categorias en ese modo.

Excel no acepta mas de 32,767 caracteres por celda; los reportes guardados
son JSON que pueden pasar de eso. En vez de cortarlos (y perder datos), un
valor largo se reparte en columnas "<columna> (parte 2)", "(parte 3)"...
que pegadas en orden reconstruyen el valor completo.
"""
from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from . import db

APP_DIR = Path(__file__).parent
SQLITE_PATH = APP_DIR / "historial.db"
JSON_LOCALES = ["recetas.json", "mapeo_manual.json", "categorias_gasto.json"]
MAX_CELDA = 32000


def _tablas_postgres() -> list[tuple[str, list[str], list[tuple]]]:
    db.inicializar_tablas()
    con = db.conectar()
    resultado = []
    with con.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        for (tabla,) in cur.fetchall():
            cur.execute(f'SELECT * FROM "{tabla}"')
            columnas = [d[0] for d in cur.description]
            resultado.append((tabla, columnas, cur.fetchall()))
    con.close()
    return resultado


def _tablas_sqlite() -> list[tuple[str, list[str], list[tuple]]]:
    if not SQLITE_PATH.exists():
        return []
    con = sqlite3.connect(SQLITE_PATH)
    resultado = []
    tablas = [t for (t,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    for tabla in tablas:
        cur = con.execute(f'SELECT * FROM "{tabla}"')
        columnas = [d[0] for d in cur.description]
        resultado.append((tabla, columnas, cur.fetchall()))
    con.close()
    return resultado


def _json_locales() -> list[tuple[str, list[str], list[tuple]]]:
    resultado = []
    for nombre in JSON_LOCALES:
        path = APP_DIR / nombre
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            datos = json.load(f)
        filas = [(k, json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v) for k, v in datos.items()] \
            if isinstance(datos, dict) else [(i, json.dumps(v, ensure_ascii=False)) for i, v in enumerate(datos)]
        resultado.append((nombre, ["clave", "valor"], filas))
    return resultado


def _celda(valor):
    if valor is None or isinstance(valor, (int, float, datetime)):
        return valor
    texto = valor if isinstance(valor, str) else str(valor)
    return ILLEGAL_CHARACTERS_RE.sub("", texto)


def _partes(valor) -> list:
    if isinstance(valor, str) and len(valor) > MAX_CELDA:
        return [valor[i:i + MAX_CELDA] for i in range(0, len(valor), MAX_CELDA)]
    return [valor]


def _escribir_hoja(wb, nombre: str, columnas: list[str], filas: list[tuple]) -> None:
    ws = wb.create_sheet(nombre[:31])
    filas_limpias = [[_partes(_celda(v)) for v in fila] for fila in filas]
    # cuantas partes necesita cada columna en el peor caso
    partes_por_col = [max([len(f[i]) for f in filas_limpias] or [1]) for i in range(len(columnas))]
    encabezado = []
    for col, n in zip(columnas, partes_por_col):
        encabezado.append(col)
        encabezado += [f"{col} (parte {k})" for k in range(2, n + 1)]
    ws.append(encabezado)
    for fila in filas_limpias:
        salida = []
        for partes, n in zip(fila, partes_por_col):
            salida += partes + [None] * (n - len(partes))
        ws.append(salida)


def generar_excel() -> tuple[bytes, dict]:
    """(contenido del .xlsx, resumen {tabla: filas})."""
    if db.usando_postgres():
        origen = "Postgres (Supabase)"
        tablas = _tablas_postgres()
    else:
        origen = "Local (SQLite + JSON)"
        tablas = _tablas_sqlite() + _json_locales()

    wb = openpyxl.Workbook()
    info = wb.active
    info.title = "Info"
    info.append(["Respaldo completo T-Grill"])
    info.append(["Generado", datetime.now().strftime("%Y-%m-%d %H:%M")])
    info.append(["Origen", origen])
    info.append([])
    info.append(["Tabla", "Filas"])
    resumen = {}
    for nombre, columnas, filas in tablas:
        _escribir_hoja(wb, nombre, columnas, filas)
        info.append([nombre, len(filas)])
        resumen[nombre] = len(filas)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue(), resumen
