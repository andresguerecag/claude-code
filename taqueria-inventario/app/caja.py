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
from pathlib import Path

from . import db

DB_PATH = Path(__file__).parent / "historial.db"
RECETAS_PATH = Path(__file__).parent / "recetas.json"

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
CATEGORIAS = [
    "Tacos", "Órdenes", "Parrilladas", "Alambres", "Piratas, gringas y campechanas",
    "Huaraches", "Costras", "Fundidos", "Quesadillas", "Tortas", "Combos",
    "Bebidas", "Extras", "Por revisar",
]
MENU_SUGERIDO = {
    "TAB": ("Tacos", "Taco de bistec"),
    "TAA": ("Tacos", "Taco de arrachera"),
    "TAP": ("Tacos", "Taco de pastor"),
    "ORDB": ("Órdenes", "Orden de bistec"),
    "ORDA": ("Órdenes", "Orden de arrachera"),
    "ORDP": ("Órdenes", "Orden de pastor"),
    "PAPS": ("Parrilladas", "Parrillada personal"),
    "PAPCQ": ("Parrilladas", "Parrillada personal con queso"),
    "PAFS": ("Parrilladas", "Parrillada familiar"),
    "PAFCQ": ("Parrilladas", "Parrillada familiar con queso"),
    "PAMS": ("Parrilladas", "Parrillada mega"),
    "PAMCQ": ("Parrilladas", "Parrillada mega con queso"),
    "ALBS": ("Alambres", "Alambre de bistec"),
    "ALBCQ": ("Alambres", "Alambre de bistec con queso"),
    "ALPS": ("Alambres", "Alambre de pastor"),
    "ALPCQ": ("Alambres", "Alambre de pastor con queso"),
    "PIRCHA": ("Piratas, gringas y campechanas", "Pirata chica"),
    "PIRGDEA": ("Piratas, gringas y campechanas", "Pirata grande"),
    "GCH": ("Piratas, gringas y campechanas", "Gringa chica"),
    "GGDE": ("Piratas, gringas y campechanas", "Gringa grande"),
    "CAMPCH": ("Piratas, gringas y campechanas", "Campechana chica"),
    "CAMPGDE": ("Piratas, gringas y campechanas", "Campechana grande"),
    "HUAB": ("Huaraches", "Huarache de bistec"),
    "HUAA": ("Huaraches", "Huarache de arrachera"),
    "HUAP": ("Huaraches", "Huarache de pastor"),
    "COSTB": ("Costras", "Costra de bistec"),
    "COSTA": ("Costras", "Costra de arrachera"),
    "COSTP": ("Costras", "Costra de pastor"),
    "FUNN": ("Fundidos", "Fundido natural"),
    "FUNB": ("Fundidos", "Fundido de bistec"),
    "FUNP": ("Fundidos", "Fundido de pastor"),
    "QUESA": ("Quesadillas", "Quesadilla"),
    "QUESACAR": ("Quesadillas", "Quesadilla con carne"),
    "DOM": ("Quesadillas", "Quesadilla con pastor"),
    "TORB": ("Tortas", "Torta de bistec"),
    "TORA": ("Tortas", "Torta de arrachera"),
    "TORP": ("Tortas", "Torta de pastor"),
    "REFALBS": ("Combos", "Completo alambre de bistec (con refresco)"),
    "REFCAMPGDE": ("Combos", "Completo gigante (campechana con refresco)"),
    "REF600": ("Bebidas", "Refresco 600 ml"),
    "AGUACIEL": ("Bebidas", "Agua Ciel"),
    "EXTRAT": ("Extras", "Extra de tortillas"),
    "EXTRAQ": ("Extras", "Extra de queso"),
    # En el Excel de recetas aparecen como "Mediana archera/mixta", pero la
    # clave PIRCH parece de "pirata chica": hay que confirmar que son.
    "PIRCH": ("Por revisar", "Mediana de arrachera"),
    "PIRCHB": ("Por revisar", "Mediana de bistec con queso"),
    "CCH": ("Por revisar", "Mediana mixta"),
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
            categoria, nombre = MENU_SUGERIDO.get(clave, (None, info["nombre"].strip().capitalize()))
            cur.execute(_sql(con, "INSERT INTO menu (clave, nombre, precio, categoria, receta, activo) VALUES (?, ?, NULL, ?, ?, 1)"),
                        (clave, nombre, categoria, clave))
    con.commit()
    _ordenar_menu_existente(con)


def _ordenar_menu_existente(con) -> None:
    """Bases que ya se habian sembrado con los nombres crudos de la tabla de
    recetas: se les pone el nombre claro y la categoria, SOLO a los platillos
    que nadie ha tocado (sin categoria y con el nombre automatico). Lo que la
    familia ya edito no se cambia. Corre una vez por arranque."""
    global _menu_ordenado
    if _menu_ordenado or not RECETAS_PATH.exists():
        return
    with open(RECETAS_PATH, encoding="utf-8") as f:
        recetas = json.load(f)
    cur = con.cursor()
    for clave, (categoria, nombre) in MENU_SUGERIDO.items():
        if clave not in recetas:
            continue
        crudo = recetas[clave]["nombre"].strip()
        for automatico in {crudo.capitalize(), crudo.title()}:
            cur.execute(_sql(con, "UPDATE menu SET nombre = ?, categoria = ? WHERE clave = ? AND categoria IS NULL AND nombre = ?"),
                        (nombre, categoria, clave, automatico))
    con.commit()
    _menu_ordenado = True


# --- menu -------------------------------------------------------------------

def _orden_categoria(categoria: str) -> tuple:
    return (CATEGORIAS.index(categoria) if categoria in CATEGORIAS else len(CATEGORIAS) - 1.5, categoria)


def listar_menu(solo_vendibles: bool = False, con_insumos: bool = False) -> list[dict]:
    filas = _consultar("SELECT clave, nombre, precio, categoria, receta, activo FROM menu")
    menu = [
        {"clave": c, "nombre": n, "precio": p, "categoria": cat or "", "receta": r or "", "activo": bool(a)}
        for c, n, p, cat, r, a in filas
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
                                 activo: bool, insumos: dict) -> str:
    """Guarda el platillo y, en el mismo paso, lo que lleva (su receta), para
    que el menu y las recetas no se editen en dos lugares distintos.
    `insumos`: {insumo: cantidad} en kg (carne/pastor/queso) o piezas; vacio
    o 0 = no lleva ese insumo."""
    from . import reconciliacion
    clave = (clave or "").strip().upper() or clave_desde_nombre(nombre)
    actual = next((m for m in listar_menu() if m["clave"] == clave), None)
    receta = (actual or {}).get("receta") or clave
    guardar_platillo(clave, nombre, precio, categoria, receta, activo)
    limpios = {k: float(v) for k, v in (insumos or {}).items()
               if k in reconciliacion.INSUMOS_VALIDOS and v not in (None, "") and float(v) > 0}
    reconciliacion.guardar_receta(receta, nombre, limpios)
    return clave


def guardar_platillo(clave: str, nombre: str, precio: float | None, categoria: str | None,
                     receta: str | None, activo: bool) -> None:
    clave = clave.strip().upper()
    if not clave or not nombre.strip():
        raise ValueError("Falta clave o nombre.")
    if precio is not None and precio < 0:
        raise ValueError("El precio no puede ser negativo.")
    _ejecutar([
        ("DELETE FROM menu WHERE clave = ?", (clave,)),
        ("INSERT INTO menu (clave, nombre, precio, categoria, receta, activo) VALUES (?, ?, ?, ?, ?, ?)",
         (clave, nombre.strip(), precio, (categoria or "").strip() or None, (receta or "").strip().upper() or None, 1 if activo else 0)),
    ])


def eliminar_platillo(clave: str) -> None:
    _ejecutar([("DELETE FROM menu WHERE clave = ?", (clave.strip().upper(),))])


# --- formas de pago -------------------------------------------------------------

def listar_formas_pago() -> list[dict]:
    filas = _consultar("SELECT forma_pago, cuenta, comision_pct, orden FROM formas_pago ORDER BY orden, forma_pago")
    return [{"forma_pago": f, "cuenta": c, "comision_pct": p, "orden": o} for f, c, p, o in filas]


def guardar_comision(forma_pago: str, comision_pct: float | None) -> None:
    if comision_pct is not None and not (0 <= comision_pct < 100):
        raise ValueError("La comisión debe estar entre 0 y 100%.")
    _ejecutar([("UPDATE formas_pago SET comision_pct = ? WHERE forma_pago = ?", (comision_pct, forma_pago))])


# --- ventas ------------------------------------------------------------------

def registrar_venta(fecha: str, hora: str, sucursal: str, forma_pago: str,
                    items: list[dict], recibido: float | None = None) -> dict:
    """items: [{clave, cantidad}]. El precio y el nombre se toman del menu
    del servidor, nunca de lo que mande el navegador."""
    sucursal = (sucursal or "").strip()
    if not sucursal:
        raise ValueError("Falta la sucursal.")
    formas = {f["forma_pago"] for f in listar_formas_pago()}
    if forma_pago not in formas:
        raise ValueError(f"Forma de pago desconocida: {forma_pago}")
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
        lineas.append((clave, p["nombre"], cantidad, p["precio"], round(cantidad * p["precio"], 2)))
    if not lineas:
        raise ValueError("La orden está vacía.")
    total = round(sum(l[4] for l in lineas), 2)
    if forma_pago == "Efectivo" and recibido is not None and recibido < total:
        raise ValueError("Lo recibido es menos que el total.")

    con = _conectar()
    cur = con.cursor()
    cur.execute(_sql(con, "SELECT COALESCE(MAX(folio), 0) FROM ventas WHERE fecha = ? AND sucursal = ?"), (fecha, sucursal))
    folio = cur.fetchone()[0] + 1
    insert = "INSERT INTO ventas (folio, fecha, hora, sucursal, forma_pago, total, recibido, estado) VALUES (?, ?, ?, ?, ?, ?, ?, 'pagada')"
    if isinstance(con, sqlite3.Connection):
        cur.execute(insert, (folio, fecha, hora, sucursal, forma_pago, total, recibido))
        venta_id = cur.lastrowid
    else:
        cur.execute(_sql(con, insert) + " RETURNING id", (folio, fecha, hora, sucursal, forma_pago, total, recibido))
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
        for fp in c["formas"].values():
            if fp["comision_pct"] is None:
                if fp["forma_pago"] != "Efectivo":
                    c["falta_comision"].append(fp["forma_pago"])
                fp["comision"] = 0.0
            else:
                fp["comision"] = round(fp["bruto"] * fp["comision_pct"] / 100, 2)
            c["comision"] += fp["comision"]
            fp["bruto"] = round(fp["bruto"], 2)
        c["bruto"] = round(c["bruto"], 2)
        c["comision"] = round(c["comision"], 2)
        c["neto_estimado"] = round(c["bruto"] - c["comision"], 2)
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
        pct = formas[forma]["comision_pct"]
        if pct:
            w.writerow([fecha_us, f"Comisión {forma} - {suc}", f"{-total * pct / 100:.2f}", "Comisiones", f"{pct}% de {total:.2f}"])
    return salida.getvalue()
