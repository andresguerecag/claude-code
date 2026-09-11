"""
Conexion opcional a Postgres (Supabase) para que el historial, las recetas
y el mapeo manual sobrevivan aunque el hosting gratis borre el disco local
en cada redeploy.

Si la variable de entorno DATABASE_URL esta configurada, todo se guarda ahi.
Si NO esta configurada (como al correr la app en tu compu para probar),
se sigue usando SQLite/JSON local -- no necesitas internet ni cuenta de
Supabase para desarrollar o probar.
"""
from __future__ import annotations

import os

DATABASE_URL = os.environ.get("DATABASE_URL")


def usando_postgres() -> bool:
    return bool(DATABASE_URL)


def conectar():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def inicializar_tablas():
    if not usando_postgres():
        return
    con = conectar()
    with con, con.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reportes (
                fecha TEXT NOT NULL,
                sucursal TEXT NOT NULL,
                reporte_json TEXT NOT NULL,
                creado_en TIMESTAMP NOT NULL DEFAULT now(),
                PRIMARY KEY (fecha, sucursal)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS recetas (
                clave TEXT PRIMARY KEY,
                nombre TEXT NOT NULL,
                receta_json TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mapeo_manual (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reportes_dinero (
                fecha TEXT NOT NULL,
                sucursal TEXT NOT NULL,
                reporte_json TEXT NOT NULL,
                creado_en TIMESTAMP NOT NULL DEFAULT now(),
                PRIMARY KEY (fecha, sucursal)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS categorias_gasto (
                nombre TEXT PRIMARY KEY,
                grupo TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mapeo_gastos (
                concepto TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS salidas_efectivo (
                id SERIAL PRIMARY KEY,
                fecha TEXT NOT NULL,
                sucursal TEXT NOT NULL,
                concepto TEXT NOT NULL,
                categoria TEXT NOT NULL,
                monto DOUBLE PRECISION NOT NULL,
                creado_en TIMESTAMP NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS colchon_inicial (
                sucursal TEXT NOT NULL,
                anio_mes TEXT NOT NULL,
                monto DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (sucursal, anio_mes)
            )
        """)
    con.close()
