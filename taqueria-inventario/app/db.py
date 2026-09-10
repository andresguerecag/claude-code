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
    con.close()
