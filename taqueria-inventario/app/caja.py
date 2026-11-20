"""
Caja de la Versión 2: el reemplazo de Wansoft para cobrar.

- Menu con precios: cada platillo que se vende, con su precio y (si aplica)
  la receta que descuenta del inventario. Se siembra con las claves de la
  tabla de recetas SIN precio: los precios los captura la familia, nunca se
  inventan. Un platillo sin precio no aparece en la caja.
- Ventas: cada cobro con sus platillos, sucursal y forma de pago.
- Formas de pago y a que cuenta llega cada una (como lo explico la familia):
    Efectivo        -> Efectivo (en caja)
    Tarjeta         -> Mercado Pago (terminal en sucursal)
    Uber/DiDi/Rappi -> BBVA (deposito semanal, ya sin comision)
  La comision de cada una se captura en la pantalla de cuentas; mientras no
  se capture, el saldo estimado se muestra en bruto y lo dice.
- Corte del dia: ventas por forma de pago y efectivo esperado vs. contado.
- Exportacion CSV para Quicken Simplifi, un archivo por cuenta.

Misma logica dual Postgres/SQLite que el resto de la app, pero con un helper
local para no repetir cada consulta dos veces.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import db

DB_PATH = Path(__file__).parent / "historial.db"
RECETAS_PATH = Path(__file__).parent / "recetas.json"

# Liquidaciones de las plataformas (Uber Eats, DiDi Food, Rappi), segun guias
# publicas de 2026 -- se pueden ajustar en pantalla con lo que diga el
# contrato o el estado de cuenta de cada una:
#   - pagan cada semana; la semana va de lunes a domingo y se deposita
#     Uber y DiDi el martes siguiente, Rappi el miercoles (o jueves);
#   - descuentan su comision + 16% de IVA sobre esa comision;
#   - retienen 2.5% de ISR y 8% de IVA sobre la venta SIN IVA (lo pagan al
#     SAT a nombre del restaurante, pero baja lo que llega al banco).
# La comision de cada contrato NO se pre-llena (depende de cada restaurante).
DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
PLATAFORMAS_DEFAULT = {
    # forma_pago: (iva_sobre_comision, ret_isr_pct, ret_iva_pct, dia_deposito 0=lunes)
    "Uber": (1, 2.5, 8.0, 1),
    "DiDi": (1, 2.5, 8.0, 1),
    "Rappi": (1, 2.5, 8.0, 2),
    "Tarjeta": (1, 0.0, 0.0, None),
}
IVA = 0.16

FORMAS_PAGO_DEFAULT = [
    # forma_pago, cuenta, orden
    ("Efectivo", "Efectivo", 1),
    ("Tarjeta", "Mercado Pago", 2),
    ("Uber", "BBVA", 3),
    ("DiDi", "BBVA", 4),
    ("Rappi", "BBVA", 5),
]

# Menu ordenado: categorias en el orden en que aparecen en la caja, y para
# cada clave (la misma de Wansoft, para que sigan cuadrando los reportes
# mientras dure el piloto) su categoria y un nombre claro. En las claves de
# Wansoft la "B" final es bistec y la "A" arrachera (TORB/TORA, ORDB/ORDA...),
# como confirman los mapeos de la familia (TORTARRACH -> TORA).
# Menu tal como esta impreso en las cartas del local (precios incluidos), y
# el precio en las apps de delivery segun el "Costeo Plataformas" de la
# familia (el mismo para Uber, DiDi y Rappi).
# Claves = las de Wansoft cuando existen, para que los reportes sigan
# cuadrando durante el piloto; los platillos que Wansoft no tenia por
# separado llevan clave nueva. En las claves de Wansoft la "B" final es
# bistec y la "A" arrachera (TORB/TORA, ORDB/ORDA...).
CATEGORIAS = [
    "Tacos", "Órdenes", "Piratas, campechanas y gringas", "Huaraches", "Alambres",
    "Queso fundido", "Parrilladas", "Costras", "Tortas", "Quesadillas",
    "Pozole y frijoles", "Promos", "Bebidas", "Extras", "Fuera del menú",
]
# clave: (categoria, nombre, precio de la carta)
MENU_SUGERIDO = {
    "TAP": ("Tacos", "Taco de pastor", 18, 23),
    "TAB": ("Tacos", "Taco de bistec", 21, 27),
    "TAA": ("Tacos", "Taco de arrachera", 25, 33),
    "ORDP": ("Órdenes", "Orden de 5 tacos de pastor", 85, 110),
    "ORDB": ("Órdenes", "Orden de 5 tacos de bistec", 98, 130),
    "ORDA": ("Órdenes", "Orden de 5 tacos de arrachera", 105, 150),
    "PIRCHA": ("Piratas, campechanas y gringas", "Pirata mediana", 70, 78),
    "PIRGDEA": ("Piratas, campechanas y gringas", "Pirata gigante", 120, 145),
    "CAMPCH": ("Piratas, campechanas y gringas", "Campechana mediana", 70, 65),
    "CAMPGDE": ("Piratas, campechanas y gringas", "Campechana gigante", 115, 145),
    "GCH": ("Piratas, campechanas y gringas", "Gringa mediana", 65, 78),
    "GGDE": ("Piratas, campechanas y gringas", "Gringa gigante", 95, 145),
    "HUAP": ("Huaraches", "Huarache de pastor", 80, 89),
    "HUAB": ("Huaraches", "Huarache de bistec", 95, 103),
    "HUAA": ("Huaraches", "Huarache de arrachera", 98, 118),
    "ALPS": ("Alambres", "Alambre de pastor", 95, 115),
    "ALPCQ": ("Alambres", "Alambre de pastor con queso", 105, 130),
    "ALBS": ("Alambres", "Alambre de bistec", 110, 130),
    "ALBCQ": ("Alambres", "Alambre de bistec con queso", 120, 145),
    "ALAS": ("Alambres", "Alambre de arrachera", 120, 130),
    "ALACQ": ("Alambres", "Alambre de arrachera con queso", 125, 145),
    "FUNN": ("Queso fundido", "Queso fundido natural", 80, 90),
    "FUNP": ("Queso fundido", "Queso fundido con pastor", 95, 110),
    "FUNB": ("Queso fundido", "Queso fundido con bistec", 95, 110),
    "FUNA": ("Queso fundido", "Queso fundido con arrachera", 95, 110),
    "PAPS": ("Parrilladas", "Parrillada personal", 120, 159),
    "PAPCQ": ("Parrilladas", "Parrillada personal con queso", 130, 174),
    "PAFS": ("Parrilladas", "Parrillada grande", 240, 320),
    "PAFCQ": ("Parrilladas", "Parrillada grande con queso", 270, 361),
    "PAMS": ("Parrilladas", "Parrillada mega", 390, 420),
    "PAMCQ": ("Parrilladas", "Parrillada mega con queso", 425, 461),
    "COSTP": ("Costras", "Costra de queso con pastor", 85, 90),
    "COSTB": ("Costras", "Costra de queso con bistec", 85, 90),
    "COSTA": ("Costras", "Costra de queso con arrachera", 85, 90),
    "TORP": ("Tortas", "Torta de pastor", 100, 135),
    "TORB": ("Tortas", "Torta de bistec", 110, 145),
    "TORA": ("Tortas", "Torta de arrachera", 115, 145),
    "QUESA": ("Quesadillas", "Quesadilla natural", 60, 49),
    "QUESACAR": ("Quesadillas", "Quesadilla con bistec o arrachera", 85, 75),
    "DOM": ("Quesadillas", "Quesadilla con pastor", 85, 75),
    "POZ500": ("Pozole y frijoles", "Pozole grande (500 g)", 100, None),
    "POZLITRO": ("Pozole y frijoles", "Pozole litro para llevar", 185, None),
    "FRIJOLES": ("Pozole y frijoles", "Frijoles charros (tazón)", 25, None),
    "PROMO2ORDP": ("Promos", "2 órdenes de pastor y 2 aguas", 150, None),
    "REFCAMPGDE": ("Promos", "Campechana, charros y refresco", 160, None),
    "REFALBS": ("Promos", "Alambre, charros y refresco", 160, None),
    "REF600": ("Bebidas", "Refresco 600 ml", 35, 40),
    "AGUACIEL": ("Bebidas", "Agua Ciel", 35, None),
    "FUZETEA": ("Bebidas", "Fuze tea 600 ml", 35, None),
    "AGUACATE": ("Extras", "Aguacate", 15, None),
    "GUACAMOLE": ("Extras", "Guacamole", 25, 25),
    "EXTRAQ": ("Extras", "Queso extra", None, None),
    "EXTRAT": ("Extras", "Tortillas extra", None, None),
    # En la tabla de recetas pero no en la carta impresa: quedan fuera de venta.
    "PIRCH": ("Fuera del menú", "Mediana de arrachera", None, None),
    "PIRCHB": ("Fuera del menú", "Mediana de bistec con queso", None, None),
    "CCH": ("Fuera del menú", "Mediana mixta", None, None),
}
FUERA_DEL_MENU = {"PIRCH", "PIRCHB", "CCH"}
# Formas de pago de las apps: ahi se cobra el "precio en apps" del platillo.
PLATAFORMAS = {"Uber", "DiDi", "Rappi"}
# Platillos que la tabla de recetas no tenia: se crean con lo que llevan
# (mismos gramos que su equivalente de bistec; el pozole, los frijoles y el
# aguacate no usan insumos que se cuenten en el inventario).
RECETAS_NUEVAS = {
    "ALAS": {"carne": 0.12, "tortillas": 7.0},
    "ALACQ": {"carne": 0.12, "queso": 0.05, "tortillas": 7.0},
    "FUNA": {"carne": 0.12, "queso": 0.15, "tortillas": 7.0},
    "POZ500": {}, "POZLITRO": {}, "FRIJOLES": {},
    "PROMO2ORDP": {"pastor": 0.30, "tortillas": 10.0},
    "FUZETEA": {"refrescos": 1.0},
    "AGUACATE": {},
    "GUACAMOLE": {},
}
# Nombres que se pusieron en el deploy anterior (DP #3): si un platillo
# todavia tiene ese nombre, nadie lo ha cambiado y se puede actualizar.
_NOMBRES_ANTERIORES = {
    "TAB": "Taco de bistec",
    "TAA": "Taco de arrachera",
    "TAP": "Taco de pastor",
    "ORDB": "Orden de bistec",
    "ORDA": "Orden de arrachera",
    "ORDP": "Orden de pastor",
    "PAPS": "Parrillada personal",
    "PAPCQ": "Parrillada personal con queso",
    "PAFS": "Parrillada familiar",
    "PAFCQ": "Parrillada familiar con queso",
    "PAMS": "Parrillada mega",
    "PAMCQ": "Parrillada mega con queso",
    "ALBS": "Alambre de bistec",
    "ALBCQ": "Alambre de bistec con queso",
    "ALPS": "Alambre de pastor",
    "ALPCQ": "Alambre de pastor con queso",
    "PIRCHA": "Pirata chica",
    "PIRGDEA": "Pirata grande",
    "GCH": "Gringa chica",
    "GGDE": "Gringa grande",
    "CAMPCH": "Campechana chica",
    "CAMPGDE": "Campechana grande",
    "HUAB": "Huarache de bistec",
    "HUAA": "Huarache de arrachera",
    "HUAP": "Huarache de pastor",
    "COSTB": "Costra de bistec",
    "COSTA": "Costra de arrachera",
    "COSTP": "Costra de pastor",
    "FUNN": "Fundido natural",
    "FUNB": "Fundido de bistec",
    "FUNP": "Fundido de pastor",
    "QUESA": "Quesadilla",
    "QUESACAR": "Quesadilla con carne",
    "DOM": "Quesadilla con pastor",
    "TORB": "Torta de bistec",
    "TORA": "Torta de arrachera",
    "TORP": "Torta de pastor",
    "REFALBS": "Completo alambre de bistec (con refresco)",
    "REFCAMPGDE": "Completo gigante (campechana con refresco)",
    "REF600": "Refresco 600 ml",
    "AGUACIEL": "Agua Ciel",
    "EXTRAT": "Extra de tortillas",
    "EXTRAQ": "Extra de queso",
    "PIRCH": "Mediana de arrachera",
    "PIRCHB": "Mediana de bistec con queso",
    "CCH": "Mediana mixta",
}
_menu_ordenado = False

_DDL = [
    """CREATE TABLE IF NOT EXISTS menu (
        clave TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        precio {real},
        categoria TEXT,
        receta TEXT,
        activo INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS ventas (
        id {serial},
        folio INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        hora TEXT NOT NULL,
        sucursal TEXT NOT NULL,
        forma_pago TEXT NOT NULL,
        total {real} NOT NULL,
        recibido {real},
        estado TEXT NOT NULL DEFAULT 'pagada',
        creado_en {ts}
    )""",
    """CREATE TABLE IF NOT EXISTS venta_items (
        id {serial},
        venta_id INTEGER NOT NULL,
        clave TEXT NOT NULL,
        nombre TEXT NOT NULL,
        cantidad {real} NOT NULL,
        precio_unitario {real} NOT NULL,
        importe {real} NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS formas_pago (
        forma_pago TEXT PRIMARY KEY,
        cuenta TEXT NOT NULL,
        comision_pct {real},
        orden INTEGER NOT NULL DEFAULT 99
    )""",
    """CREATE TABLE IF NOT EXISTS liquidaciones (
        forma_pago TEXT NOT NULL,
        semana TEXT NOT NULL,
        monto {real} NOT NULL,
        fecha TEXT,
        notas TEXT,
        PRIMARY KEY (forma_pago, semana)
    )""",
    """CREATE TABLE IF NOT EXISTS cortes_caja (
        fecha TEXT NOT NULL,
        sucursal TEXT NOT NULL,
        efectivo_contado {real} NOT NULL,
        notas TEXT,
        creado_en {ts},
        PRIMARY KEY (fecha, sucursal)
    )""",
]


# --- acceso a datos (SQLite local o Postgres) ------------------------------

def _conectar():
    if db.usando_postgres():
        con = db.conectar()
        tipos = {"real": "DOUBLE PRECISION", "serial": "SERIAL PRIMARY KEY", "ts": "TIMESTAMP NOT NULL DEFAULT now()"}
    else:
        con = sqlite3.connect(DB_PATH)
        tipos = {"real": "REAL", "serial": "INTEGER PRIMARY KEY AUTOINCREMENT", "ts": "TEXT NOT NULL DEFAULT (datetime('now'))"}
    cur = con.cursor()
    for ddl in _DDL:
        cur.execute(ddl.format(**tipos))
    # id_local: identificador que pone la caja a cada venta, para que una
    # venta cobrada sin internet y reenviada al volver no se duplique.
    if isinstance(con, sqlite3.Connection):
        if "id_local" not in {fila[1] for fila in cur.execute("PRAGMA table_info(ventas)")}:
            cur.execute("ALTER TABLE ventas ADD COLUMN id_local TEXT")
    else:
        cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS id_local TEXT")
    # Precio del platillo en las apps de delivery.
    if isinstance(con, sqlite3.Connection):
        if "precio_plataforma" not in {fila[1] for fila in cur.execute("PRAGMA table_info(menu)")}:
            cur.execute(f"ALTER TABLE menu ADD COLUMN precio_plataforma {tipos['real']}")
    else:
        cur.execute(f"ALTER TABLE menu ADD COLUMN IF NOT EXISTS precio_plataforma {tipos['real']}")
    # Datos de liquidacion de cada forma de pago (ver PLATAFORMAS_DEFAULT).
    nuevas = {"iva_comision": "INTEGER", "ret_isr_pct": tipos["real"], "ret_iva_pct": tipos["real"], "dia_deposito": "INTEGER"}
    if isinstance(con, sqlite3.Connection):
        existentes = {fila[1] for fila in cur.execute("PRAGMA table_info(formas_pago)")}
        for col, tipo in nuevas.items():
            if col not in existentes:
                cur.execute(f"ALTER TABLE formas_pago ADD COLUMN {col} {tipo}")
    else:
        for col, tipo in nuevas.items():
            cur.execute(f"ALTER TABLE formas_pago ADD COLUMN IF NOT EXISTS {col} {tipo}")
    for forma, (iva_c, isr, iva_r, dia) in PLATAFORMAS_DEFAULT.items():
        cur.execute(_sql(con, "UPDATE formas_pago SET iva_comision = ? WHERE forma_pago = ? AND iva_comision IS NULL"), (iva_c, forma))
        cur.execute(_sql(con, "UPDATE formas_pago SET ret_isr_pct = ? WHERE forma_pago = ? AND ret_isr_pct IS NULL"), (isr, forma))
        cur.execute(_sql(con, "UPDATE formas_pago SET ret_iva_pct = ? WHERE forma_pago = ? AND ret_iva_pct IS NULL"), (iva_r, forma))
        if dia is not None:
            cur.execute(_sql(con, "UPDATE formas_pago SET dia_deposito = ? WHERE forma_pago = ? AND dia_deposito IS NULL"), (dia, forma))
    con.commit()
    _sembrar(con)
    return con


def _sql(con, consulta: str) -> str:
    return consulta if isinstance(con, sqlite3.Connection) else consulta.replace("?", "%s")


def _consultar(consulta: str, params: tuple = ()) -> list[tuple]:
    con = _conectar()
    cur = con.cursor()
    cur.execute(_sql(con, consulta), params)
    filas = cur.fetchall()
    con.close()
    return filas


def _ejecutar(consultas: list[tuple[str, tuple]]) -> None:
    con = _conectar()
    cur = con.cursor()
    for consulta, params in consultas:
        cur.execute(_sql(con, consulta), params)
    con.commit()
    con.close()


def _sembrar(con) -> None:
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM formas_pago")
    if cur.fetchone()[0] == 0:
        for forma, cuenta, orden in FORMAS_PAGO_DEFAULT:
            cur.execute(_sql(con, "INSERT INTO formas_pago (forma_pago, cuenta, comision_pct, orden) VALUES (?, ?, NULL, ?)"),
                        (forma, cuenta, orden))
    cur.execute("SELECT COUNT(*) FROM menu")
    if cur.fetchone()[0] == 0 and RECETAS_PATH.exists():
        with open(RECETAS_PATH, encoding="utf-8") as f:
            recetas = json.load(f)
        for clave, info in recetas.items():
            categoria, nombre, precio, precio_apps = MENU_SUGERIDO.get(clave, (None, info["nombre"].strip().capitalize(), None, None))
            cur.execute(_sql(con, "INSERT INTO menu (clave, nombre, precio, categoria, receta, activo, precio_plataforma) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                        (clave, nombre, precio, categoria, clave, 0 if clave in FUERA_DEL_MENU else 1, precio_apps))
    con.commit()
    _ordenar_menu_existente(con)


def _ordenar_menu_existente(con) -> None:
    """Pone el menu de la carta en bases ya sembradas, sin pisar lo que la
    familia ya capturo:
      - agrega los platillos de la carta que falten (con su receta);
      - pone nombre y categoria de la carta solo a los platillos que siguen
        con el nombre automatico (el crudo de la tabla de recetas o el del
        deploy anterior);
      - pone el precio de la carta solo donde todavia no hay precio;
      - saca de venta los que no estan en la carta, si nadie les puso precio.
    Corre una vez por arranque y se puede repetir sin problema."""
    global _menu_ordenado
    if _menu_ordenado:
        return
    recetas_json = {}
    if RECETAS_PATH.exists():
        with open(RECETAS_PATH, encoding="utf-8") as f:
            recetas_json = json.load(f)
    cur = con.cursor()
    cur.execute("SELECT clave, nombre FROM menu")
    existentes = dict(cur.fetchall())
    nuevas_recetas = []
    for clave, (categoria, nombre, precio, precio_apps) in MENU_SUGERIDO.items():
        if clave not in existentes:
            if clave not in recetas_json and clave not in RECETAS_NUEVAS:
                continue
            cur.execute(_sql(con, "INSERT INTO menu (clave, nombre, precio, categoria, receta, activo, precio_plataforma) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                        (clave, nombre, precio, categoria, clave, 0 if clave in FUERA_DEL_MENU else 1, precio_apps))
            if clave in RECETAS_NUEVAS:
                nuevas_recetas.append((clave, nombre))
            continue
        automaticos = {_NOMBRES_ANTERIORES.get(clave)}
        if clave in recetas_json:
            crudo = recetas_json[clave]["nombre"].strip()
            automaticos |= {crudo.capitalize(), crudo.title()}
        if existentes[clave] in automaticos:
            cur.execute(_sql(con, "UPDATE menu SET nombre = ?, categoria = ? WHERE clave = ?"), (nombre, categoria, clave))
        if precio is not None:
            cur.execute(_sql(con, "UPDATE menu SET precio = ? WHERE clave = ? AND precio IS NULL"), (precio, clave))
        if precio_apps is not None:
            cur.execute(_sql(con, "UPDATE menu SET precio_plataforma = ? WHERE clave = ? AND precio_plataforma IS NULL"), (precio_apps, clave))
        if clave in FUERA_DEL_MENU:
            cur.execute(_sql(con, "UPDATE menu SET activo = 0, categoria = ? WHERE clave = ? AND precio IS NULL"), (categoria, clave))
    con.commit()
    _menu_ordenado = True
    if nuevas_recetas:
        from . import reconciliacion
        actuales = reconciliacion._cargar_recetas()
        for clave, nombre in nuevas_recetas:
            if clave not in actuales:
                reconciliacion.guardar_receta(clave, nombre, RECETAS_NUEVAS[clave])


# --- menu -------------------------------------------------------------------

def _orden_categoria(categoria: str) -> tuple:
    return (CATEGORIAS.index(categoria) if categoria in CATEGORIAS else len(CATEGORIAS) - 1.5, categoria)


def listar_menu(solo_vendibles: bool = False, con_insumos: bool = False) -> list[dict]:
    filas = _consultar("SELECT clave, nombre, precio, categoria, receta, activo, precio_plataforma FROM menu")
    menu = [
        {"clave": c, "nombre": n, "precio": p, "categoria": cat or "", "receta": r or "", "activo": bool(a),
         "precio_plataforma": pp}
        for c, n, p, cat, r, a, pp in filas
    ]
    orden = {clave: i for i, clave in enumerate(MENU_SUGERIDO)}
    menu.sort(key=lambda m: (_orden_categoria(m["categoria"]), orden.get(m["clave"], 999), m["nombre"]))
    if solo_vendibles:
        menu = [m for m in menu if m["activo"] and m["precio"] is not None and m["precio"] > 0]
    if con_insumos:
        from .reconciliacion import _cargar_recetas
        recetas = _cargar_recetas()
        for m in menu:
            m["insumos"] = (recetas.get(m["receta"] or m["clave"]) or {}).get("receta_por_unidad", {})
    return menu


def clave_desde_nombre(nombre: str) -> str:
    """Clave automatica para un platillo nuevo: el nombre sin acentos ni
    espacios, en mayusculas (ej. 'Agua de jamaica' -> 'AGUADEJAMAICA')."""
    import unicodedata
    base = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return "".join(ch for ch in base.upper() if ch.isalnum())[:20]


def guardar_platillo_con_insumos(clave: str | None, nombre: str, categoria: str | None, precio: float | None,
                                 activo: bool, insumos: dict, precio_plataforma: float | None = None) -> str:
    """Guarda el platillo y, en el mismo paso, lo que lleva (su receta), para
    que el menu y las recetas no se editen en dos lugares distintos.
    `insumos`: {insumo: cantidad} en kg (carne/pastor/queso) o piezas; vacio
    o 0 = no lleva ese insumo."""
    from . import reconciliacion
    clave = (clave or "").strip().upper() or clave_desde_nombre(nombre)
    actual = next((m for m in listar_menu() if m["clave"] == clave), None)
    receta = (actual or {}).get("receta") or clave
    guardar_platillo(clave, nombre, precio, categoria, receta, activo, precio_plataforma)
    limpios = {k: float(v) for k, v in (insumos or {}).items()
               if k in reconciliacion.INSUMOS_VALIDOS and v not in (None, "") and float(v) > 0}
    reconciliacion.guardar_receta(receta, nombre, limpios)
    return clave


_SIN_CAMBIO = object()


def guardar_platillo(clave: str, nombre: str, precio: float | None, categoria: str | None,
                     receta: str | None, activo: bool, precio_plataforma=_SIN_CAMBIO) -> None:
    """`precio_plataforma`: precio en Uber/DiDi/Rappi (None = el mismo que en
    el local). Si no se manda, se conserva el que ya tenía."""
    clave = clave.strip().upper()
    if not clave or not nombre.strip():
        raise ValueError("Falta clave o nombre.")
    if precio is not None and precio < 0:
        raise ValueError("El precio no puede ser negativo.")
    if precio_plataforma is _SIN_CAMBIO:
        actual = _consultar("SELECT precio_plataforma FROM menu WHERE clave = ?", (clave,))
        precio_plataforma = actual[0][0] if actual else None
    if precio_plataforma is not None and precio_plataforma < 0:
        raise ValueError("El precio en apps no puede ser negativo.")
    _ejecutar([
        ("DELETE FROM menu WHERE clave = ?", (clave,)),
        ("INSERT INTO menu (clave, nombre, precio, categoria, receta, activo, precio_plataforma) VALUES (?, ?, ?, ?, ?, ?, ?)",
         (clave, nombre.strip(), precio, (categoria or "").strip() or None, (receta or "").strip().upper() or None,
          1 if activo else 0, precio_plataforma)),
    ])


def eliminar_platillo(clave: str) -> None:
    _ejecutar([("DELETE FROM menu WHERE clave = ?", (clave.strip().upper(),))])


# --- formas de pago -------------------------------------------------------------

def listar_formas_pago() -> list[dict]:
    filas = _consultar("SELECT forma_pago, cuenta, comision_pct, orden, iva_comision, ret_isr_pct, ret_iva_pct, dia_deposito "
                       "FROM formas_pago ORDER BY orden, forma_pago")
    return [{"forma_pago": f, "cuenta": c, "comision_pct": p, "orden": o, "iva_comision": bool(ivc),
             "ret_isr_pct": isr or 0.0, "ret_iva_pct": ivr or 0.0, "dia_deposito": dia,
             "dia_deposito_nombre": DIAS_SEMANA[dia] if dia is not None else None}
            for f, c, p, o, ivc, isr, ivr, dia in filas]


def guardar_comision(forma_pago: str, comision_pct: float | None, iva_comision: bool | None = None,
                     ret_isr_pct: float | None = None, ret_iva_pct: float | None = None,
                     dia_deposito: int | None = None) -> None:
    for nombre, v in (("La comisión", comision_pct), ("La retención de ISR", ret_isr_pct), ("La retención de IVA", ret_iva_pct)):
        if v is not None and not (0 <= v < 100):
            raise ValueError(f"{nombre} debe estar entre 0 y 100%.")
    if dia_deposito is not None and dia_deposito not in range(7):
        raise ValueError("Día de depósito inválido.")
    actual = next((f for f in listar_formas_pago() if f["forma_pago"] == forma_pago), None)
    if actual is None:
        raise ValueError("Forma de pago desconocida.")
    _ejecutar([(
        "UPDATE formas_pago SET comision_pct = ?, iva_comision = ?, ret_isr_pct = ?, ret_iva_pct = ?, dia_deposito = ? WHERE forma_pago = ?",
        (comision_pct,
         int(actual["iva_comision"] if iva_comision is None else iva_comision),
         actual["ret_isr_pct"] if ret_isr_pct is None else ret_isr_pct,
         actual["ret_iva_pct"] if ret_iva_pct is None else ret_iva_pct,
         actual["dia_deposito"] if dia_deposito is None else dia_deposito,
         forma_pago),
    )])


def descuentos(forma: dict, bruto: float) -> dict:
    """Lo que la plataforma o la terminal descuenta de una venta `bruto`
    (precio cobrado, con IVA incluido) antes de depositar."""
    pct = forma.get("comision_pct")
    comision = bruto * pct / 100 if pct else 0.0
    iva_comision = comision * IVA if forma.get("iva_comision") else 0.0
    sin_iva = bruto / (1 + IVA)
    retenciones = sin_iva * ((forma.get("ret_isr_pct") or 0) + (forma.get("ret_iva_pct") or 0)) / 100
    return {
        "comision": round(comision + iva_comision, 2),
        "retenciones": round(retenciones, 2),
        "neto": round(bruto - comision - iva_comision - retenciones, 2),
        "falta_comision": pct is None and forma.get("forma_pago") != "Efectivo",
    }


# --- ventas ------------------------------------------------------------------

def registrar_venta(fecha: str, hora: str, sucursal: str, forma_pago: str,
                    items: list[dict], recibido: float | None = None, id_local: str | None = None) -> dict:
    """items: [{clave, cantidad}]. El precio y el nombre se toman del menu
    del servidor, nunca de lo que mande el navegador.

    Con `id_local` (ventas de la caja, incluidas las cobradas sin internet)
    la operación es idempotente: si esa venta ya se registró, se regresa la
    misma en vez de duplicarla. Una venta que se cobró sin internet ya se
    cobró de verdad, así que se acepta aunque el platillo se haya quitado de
    la venta mientras tanto (mientras siga en el menú con precio)."""
    id_local = (id_local or "").strip()[:64] or None
    if id_local:
        ya = _consultar("SELECT id FROM ventas WHERE id_local = ?", (id_local,))
        if ya:
            return obtener_venta(ya[0][0])
    sucursal = (sucursal or "").strip()
    if not sucursal:
        raise ValueError("Falta la sucursal.")
    formas = {f["forma_pago"] for f in listar_formas_pago()}
    if forma_pago not in formas:
        raise ValueError(f"Forma de pago desconocida: {forma_pago}")
    if id_local:
        menu = {m["clave"]: m for m in listar_menu() if m["precio"] is not None and m["precio"] > 0}
    else:
        menu = {m["clave"]: m for m in listar_menu(solo_vendibles=True)}

    lineas = []
    for it in items:
        clave = str(it.get("clave", "")).strip().upper()
        cantidad = float(it.get("cantidad") or 0)
        if cantidad <= 0:
            continue
        if clave not in menu:
            raise ValueError(f"El platillo {clave} no está en el menú o no tiene precio.")
        p = menu[clave]
        precio = p["precio_plataforma"] if forma_pago in PLATAFORMAS and p["precio_plataforma"] else p["precio"]
        lineas.append((clave, p["nombre"], cantidad, precio, round(cantidad * precio, 2)))
    if not lineas:
        raise ValueError("La orden está vacía.")
    total = round(sum(l[4] for l in lineas), 2)
    if forma_pago == "Efectivo" and recibido is not None and recibido < total:
        raise ValueError("Lo recibido es menos que el total.")

    con = _conectar()
    cur = con.cursor()
    cur.execute(_sql(con, "SELECT COALESCE(MAX(folio), 0) FROM ventas WHERE fecha = ? AND sucursal = ?"), (fecha, sucursal))
    folio = cur.fetchone()[0] + 1
    insert = "INSERT INTO ventas (folio, fecha, hora, sucursal, forma_pago, total, recibido, estado, id_local) VALUES (?, ?, ?, ?, ?, ?, ?, 'pagada', ?)"
    if isinstance(con, sqlite3.Connection):
        cur.execute(insert, (folio, fecha, hora, sucursal, forma_pago, total, recibido, id_local))
        venta_id = cur.lastrowid
    else:
        cur.execute(_sql(con, insert) + " RETURNING id", (folio, fecha, hora, sucursal, forma_pago, total, recibido, id_local))
        venta_id = cur.fetchone()[0]
    for l in lineas:
        cur.execute(_sql(con, "INSERT INTO venta_items (venta_id, clave, nombre, cantidad, precio_unitario, importe) VALUES (?, ?, ?, ?, ?, ?)"),
                    (venta_id, *l))
    con.commit()
    con.close()
    return obtener_venta(venta_id)


def obtener_venta(venta_id: int) -> dict | None:
    filas = _consultar("SELECT id, folio, fecha, hora, sucursal, forma_pago, total, recibido, estado FROM ventas WHERE id = ?", (venta_id,))
    if not filas:
        return None
    i, folio, fecha, hora, suc, forma, total, recibido, estado = filas[0]
    items = _consultar("SELECT clave, nombre, cantidad, precio_unitario, importe FROM venta_items WHERE venta_id = ? ORDER BY id", (i,))
    return {
        "id": i, "folio": folio, "fecha": fecha, "hora": hora, "sucursal": suc, "forma_pago": forma,
        "total": total, "recibido": recibido,
        "cambio": round(recibido - total, 2) if recibido is not None else None, "estado": estado,
        "items": [{"clave": c, "nombre": n, "cantidad": q, "precio_unitario": p, "importe": imp} for c, n, q, p, imp in items],
    }


def listar_ventas(fecha: str, sucursal: str | None = None) -> list[dict]:
    if sucursal:
        filas = _consultar("SELECT id FROM ventas WHERE fecha = ? AND sucursal = ? ORDER BY id DESC", (fecha, sucursal))
    else:
        filas = _consultar("SELECT id FROM ventas WHERE fecha = ? ORDER BY id DESC", (fecha,))
    return [obtener_venta(i) for (i,) in filas]


def cancelar_venta(venta_id: int) -> None:
    _ejecutar([("UPDATE ventas SET estado = 'cancelada' WHERE id = ?", (venta_id,))])


def sucursales() -> list[str]:
    return [s for (s,) in _consultar("SELECT DISTINCT sucursal FROM ventas ORDER BY sucursal")]


# --- corte del dia ---------------------------------------------------------------

def _salidas_efectivo_del_dia(fecha: str, sucursal: str) -> float:
    """Salidas de efectivo registradas ese dia en la Versión 1 (colchón)."""
    try:
        from . import colchon
        if db.usando_postgres():
            con = db.conectar()
            with con.cursor() as cur:
                cur.execute("SELECT COALESCE(SUM(monto), 0) FROM salidas_efectivo WHERE fecha = %s AND sucursal = %s", (fecha, sucursal))
                total = cur.fetchone()[0]
            con.close()
        else:
            con = colchon._conectar_sqlite()
            total = con.execute("SELECT COALESCE(SUM(monto), 0) FROM salidas_efectivo WHERE fecha = ? AND sucursal = ?", (fecha, sucursal)).fetchone()[0]
            con.close()
        return float(total or 0)
    except Exception:
        return 0.0


def corte_del_dia(fecha: str, sucursal: str) -> dict:
    ventas = [v for v in listar_ventas(fecha, sucursal) if v["estado"] == "pagada"]
    canceladas = [v for v in listar_ventas(fecha, sucursal) if v["estado"] == "cancelada"]
    formas = listar_formas_pago()
    por_forma = []
    for f in formas:
        vs = [v for v in ventas if v["forma_pago"] == f["forma_pago"]]
        por_forma.append({
            "forma_pago": f["forma_pago"], "cuenta": f["cuenta"],
            "num_ventas": len(vs), "total": round(sum(v["total"] for v in vs), 2),
        })

    platillos: dict[str, dict] = {}
    for v in ventas:
        for it in v["items"]:
            p = platillos.setdefault(it["clave"], {"clave": it["clave"], "nombre": it["nombre"], "cantidad": 0.0, "importe": 0.0})
            p["cantidad"] += it["cantidad"]
            p["importe"] = round(p["importe"] + it["importe"], 2)

    efectivo_ventas = next((f["total"] for f in por_forma if f["forma_pago"] == "Efectivo"), 0.0)
    salidas = _salidas_efectivo_del_dia(fecha, sucursal)
    esperado = round(efectivo_ventas - salidas, 2)

    filas = _consultar("SELECT efectivo_contado, notas FROM cortes_caja WHERE fecha = ? AND sucursal = ?", (fecha, sucursal))
    contado = filas[0][0] if filas else None
    return {
        "fecha": fecha, "sucursal": sucursal,
        "num_ventas": len(ventas), "num_canceladas": len(canceladas),
        "total_vendido": round(sum(v["total"] for v in ventas), 2),
        "por_forma_pago": por_forma,
        "platillos": sorted(platillos.values(), key=lambda p: p["importe"], reverse=True),
        "efectivo": {
            "ventas_en_efectivo": efectivo_ventas,
            "salidas_registradas": salidas,
            "esperado": esperado,
            "contado": contado,
            "diferencia": round(contado - esperado, 2) if contado is not None else None,
            "notas": filas[0][1] if filas else None,
        },
    }


def guardar_conteo_efectivo(fecha: str, sucursal: str, contado: float, notas: str = "") -> None:
    _ejecutar([
        ("DELETE FROM cortes_caja WHERE fecha = ? AND sucursal = ?", (fecha, sucursal)),
        ("INSERT INTO cortes_caja (fecha, sucursal, efectivo_contado, notas) VALUES (?, ?, ?, ?)", (fecha, sucursal, contado, notas)),
    ])


def ventas_para_inventario(fecha: str, sucursal: str) -> list:
    """Lo vendido en la caja ese dia, en el mismo formato que el reporte de
    Wansoft, para calcular el consumo teorico con las mismas recetas. Cada
    platillo usa la receta con la que esta ligado en el menu (o su propia
    clave si no tiene receta ligada: entonces sale como pendiente)."""
    from .reconciliacion import VentaPlatillo
    menu = {m["clave"]: m for m in listar_menu()}
    sumas: dict[str, list] = {}
    for v in listar_ventas(fecha, sucursal):
        if v["estado"] != "pagada":
            continue
        for it in v["items"]:
            receta = (menu.get(it["clave"]) or {}).get("receta") or it["clave"]
            fila = sumas.setdefault(receta, [it["nombre"], 0.0])
            fila[1] += it["cantidad"]
    return [VentaPlatillo(clave=clave, nombre=nombre, cantidad=cantidad) for clave, (nombre, cantidad) in sumas.items()]


# --- cuentas y Quicken -----------------------------------------------------------

def _ventas_periodo(desde: str, hasta: str, sucursal: str | None) -> list[tuple]:
    consulta = "SELECT fecha, sucursal, forma_pago, total FROM ventas WHERE estado = 'pagada' AND fecha >= ? AND fecha <= ?"
    params: tuple = (desde, hasta)
    if sucursal:
        consulta += " AND sucursal = ?"
        params += (sucursal,)
    return _consultar(consulta + " ORDER BY fecha", params)


def saldos_estimados(desde: str, hasta: str, sucursal: str | None = None) -> dict:
    """Por cuenta: bruto vendido, comision estimada y neto que deberia llegar."""
    formas = {f["forma_pago"]: f for f in listar_formas_pago()}
    cuentas: dict[str, dict] = {}
    for fecha, suc, forma, total in _ventas_periodo(desde, hasta, sucursal):
        f = formas.get(forma, {"cuenta": "Sin cuenta", "comision_pct": None})
        c = cuentas.setdefault(f["cuenta"], {"cuenta": f["cuenta"], "bruto": 0.0, "comision": 0.0, "formas": {}, "falta_comision": []})
        c["bruto"] += total
        fp = c["formas"].setdefault(forma, {"forma_pago": forma, "bruto": 0.0, "comision_pct": f["comision_pct"]})
        fp["bruto"] += total
    for c in cuentas.values():
        c["retenciones"] = 0.0
        for fp in c["formas"].values():
            d = descuentos(formas.get(fp["forma_pago"], {"forma_pago": fp["forma_pago"]}), fp["bruto"])
            if d["falta_comision"]:
                c["falta_comision"].append(fp["forma_pago"])
            fp["comision"], fp["retenciones"] = d["comision"], d["retenciones"]
            c["comision"] += d["comision"]
            c["retenciones"] += d["retenciones"]
            fp["bruto"] = round(fp["bruto"], 2)
        c["bruto"] = round(c["bruto"], 2)
        c["comision"] = round(c["comision"], 2)
        c["retenciones"] = round(c["retenciones"], 2)
        c["neto_estimado"] = round(c["bruto"] - c["comision"] - c["retenciones"], 2)
        c["formas"] = list(c["formas"].values())
    orden = {"BBVA": 0, "Mercado Pago": 1, "Efectivo": 2}
    return {"desde": desde, "hasta": hasta, "sucursal": sucursal,
            "cuentas": sorted(cuentas.values(), key=lambda c: orden.get(c["cuenta"], 9))}


def csv_quicken(cuenta: str, desde: str, hasta: str, sucursal: str | None = None) -> str:
    """CSV para importar en Quicken Simplifi a UNA cuenta: una linea por dia,
    sucursal y forma de pago con la venta bruta, y otra negativa con la
    comision (si esta capturada). Columnas Date, Payee, Amount, Category,
    Notes, que son las que pide el importador de Simplifi."""
    formas = {f["forma_pago"]: f for f in listar_formas_pago()}
    agrupado: dict[tuple, float] = {}
    for fecha, suc, forma, total in _ventas_periodo(desde, hasta, sucursal):
        if formas.get(forma, {}).get("cuenta") != cuenta:
            continue
        agrupado[(fecha, suc, forma)] = agrupado.get((fecha, suc, forma), 0.0) + total

    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(["Date", "Payee", "Amount", "Category", "Notes"])
    for (fecha, suc, forma), total in sorted(agrupado.items()):
        a, m, d = fecha.split("-")
        fecha_us = f"{m}/{d}/{a}"
        w.writerow([fecha_us, f"Ventas {forma} - {suc}", f"{total:.2f}", "Ventas", f"T-Grill {suc}, ventas del día por {forma}"])
        d = descuentos(formas[forma], total)
        if d["comision"]:
            w.writerow([fecha_us, f"Comisión {forma} - {suc}", f"{-d['comision']:.2f}", "Comisiones",
                        f"{formas[forma]['comision_pct']}% de {total:.2f}" + (" + IVA" if formas[forma]["iva_comision"] else "")])
        if d["retenciones"]:
            w.writerow([fecha_us, f"Retenciones ISR/IVA {forma} - {suc}", f"{-d['retenciones']:.2f}", "Impuestos retenidos",
                        "Retenido por la plataforma y pagado al SAT a nombre del restaurante"])
    return salida.getvalue()


# --- liquidaciones semanales de las plataformas --------------------------------------

def _lunes(fecha: date) -> date:
    return fecha - timedelta(days=fecha.weekday())


def liquidaciones(semanas: int = 8, hoy: date | None = None) -> dict:
    """Por plataforma y por semana (lunes a domingo): cuánto se vendió,
    cuánto debe depositar y qué día, y lo que de verdad llegó (si ya se
    capturó). Solo formas de pago con día de depósito (Uber, DiDi, Rappi)."""
    hoy = hoy or date.today()
    plataformas = [f for f in listar_formas_pago() if f["dia_deposito"] is not None]
    lunes_actual = _lunes(hoy)
    inicio = lunes_actual - timedelta(weeks=semanas - 1)
    ventas = _consultar(
        "SELECT fecha, forma_pago, total FROM ventas WHERE estado = 'pagada' AND fecha >= ? AND fecha <= ?",
        (inicio.isoformat(), (lunes_actual + timedelta(days=6)).isoformat()),
    )
    reales = {(f, s): (m, fe, n) for f, s, m, fe, n in _consultar(
        "SELECT forma_pago, semana, monto, fecha, notas FROM liquidaciones WHERE semana >= ?", (inicio.isoformat(),))}
    filas = []
    for k in range(semanas):
        lunes = lunes_actual - timedelta(weeks=k)
        domingo = lunes + timedelta(days=6)
        for p in plataformas:
            bruto = sum(t for fe, fp, t in ventas if fp == p["forma_pago"] and lunes.isoformat() <= fe <= domingo.isoformat())
            real = reales.get((p["forma_pago"], lunes.isoformat()))
            if not bruto and not real:
                continue
            d = descuentos(p, bruto)
            fecha_esperada = lunes + timedelta(days=7 + p["dia_deposito"])
            if domingo >= hoy:
                estado, texto = "en_curso", "Semana en curso"
            elif real is None:
                if hoy > fecha_esperada + timedelta(days=1):
                    estado, texto = "pendiente", "No se ha registrado el depósito"
                else:
                    estado, texto = "por_llegar", f"Debe llegar el {DIAS_SEMANA[fecha_esperada.weekday()]} {fecha_esperada.day}"
            else:
                diferencia = round(real[0] - d["neto"], 2)
                margen = max(20.0, abs(d["neto"]) * 0.01)
                if abs(diferencia) <= margen:
                    estado, texto = "ok", "Llegó completo"
                else:
                    estado = "alerta"
                    texto = f"Llegaron ${abs(diferencia):,.2f} de {'menos' if diferencia < 0 else 'más'}"
            filas.append({
                "forma_pago": p["forma_pago"], "cuenta": p["cuenta"],
                "semana": lunes.isoformat(), "semana_fin": domingo.isoformat(),
                "vendido": round(bruto, 2), "comision": d["comision"], "retenciones": d["retenciones"],
                "esperado": d["neto"], "falta_comision": d["falta_comision"],
                "fecha_esperada": fecha_esperada.isoformat(),
                "real": real[0] if real else None, "fecha_real": real[1] if real else None, "notas": real[2] if real else "",
                "diferencia": round(real[0] - d["neto"], 2) if real else None,
                "estado": estado, "texto": texto,
            })
    return {"plataformas": plataformas, "filas": filas}


def guardar_liquidacion(forma_pago: str, semana: str, monto: float | None, fecha: str | None, notas: str = "") -> None:
    lunes = _lunes(date.fromisoformat(semana)).isoformat()
    if monto is None:
        _ejecutar([("DELETE FROM liquidaciones WHERE forma_pago = ? AND semana = ?", (forma_pago, lunes))])
        return
    if monto < 0:
        raise ValueError("El monto no puede ser negativo.")
    _ejecutar([
        ("DELETE FROM liquidaciones WHERE forma_pago = ? AND semana = ?", (forma_pago, lunes)),
        ("INSERT INTO liquidaciones (forma_pago, semana, monto, fecha, notas) VALUES (?, ?, ?, ?, ?)",
         (forma_pago, lunes, monto, fecha or None, notas or "")),
    ])
