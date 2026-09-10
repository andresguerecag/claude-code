"""
Motor de conciliacion de inventario diario para la taqueria.

Compara:
  - Consumo TEORICO: lo que segun las recetas debio consumirse, calculado
    a partir de lo que vendio el POS (Wansoft) ese dia.
  - Consumo REAL: inventario inicial + compras del dia - inventario final,
    tomado del "Formato de corte" que llena la sucursal cada noche.

Principio de diseno (pedido explicitamente por la familia): si un dato no
esta disponible o un platillo no se puede identificar con confianza, NUNCA
se inventa un numero -> se reporta como "no disponible" / "pendiente de
revisar", nunca como 0 ni como un valor adivinado.

Las columnas se detectan por el CONTENIDO de los encabezados, nunca por
posicion fija de celda, porque las plantillas cambian con el tiempo.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

RECETAS_PATH = Path(__file__).parent / "recetas.json"
MAPEO_MANUAL_PATH = Path(__file__).parent / "mapeo_manual.json"
IGNORAR = "IGNORAR"

# Tolerancia de merma "normal" por insumo, en las unidades del inventario.
# Segun explico la familia: carne ~400-500g de merma es normal (descongelado,
# etc.), articulos simples (refrescos, agua) deberian cuadrar exacto.
TOLERANCIAS_DEFAULT = {
    "carne": 0.5,       # kg
    "pastor": 0.5,      # kg
    "queso": 0.3,        # kg
    "tortillas": 3.0,    # piezas o kg segun el producto (ver nota abajo)
    "telera": 2,         # piezas
}

PREFIJOS_CONOCIDOS = ["DOM ", "PLATAF ", "REF "]


def _cargar_recetas() -> dict:
    with open(RECETAS_PATH, encoding="utf-8") as f:
        return json.load(f)


def cargar_mapeo_manual() -> dict:
    """Diccionario clave_normalizada -> clave_receta (o IGNORAR) que se va
    llenando con las correcciones que la familia guarda desde el reporte."""
    if not MAPEO_MANUAL_PATH.exists():
        return {}
    with open(MAPEO_MANUAL_PATH, encoding="utf-8") as f:
        return json.load(f)


def guardar_mapeo_manual(clave_wansoft: str, clave_receta_o_ignorar: str) -> None:
    mapeo = cargar_mapeo_manual()
    mapeo[_normalizar(clave_wansoft)] = clave_receta_o_ignorar
    with open(MAPEO_MANUAL_PATH, "w", encoding="utf-8") as f:
        json.dump(mapeo, f, indent=2, ensure_ascii=False)


def _normalizar(clave: str) -> str:
    return re.sub(r"\s+", "", str(clave).strip().upper())


@dataclass
class VentaPlatillo:
    clave: str
    nombre: str
    cantidad: float


@dataclass
class ItemInventario:
    producto: str
    unidad: str | None
    inicial: float | None
    entrada: float | None
    final: float | None

    @property
    def consumo_real(self) -> float | None:
        if self.inicial is None or self.final is None:
            return None
        entrada = self.entrada or 0
        return self.inicial + entrada - self.final


@dataclass
class MatchResultado:
    ventas_identificadas: list = field(default_factory=list)   # (clave_original, clave_receta, cantidad_efectiva)
    ventas_sin_identificar: list = field(default_factory=list)  # VentaPlatillo
    ventas_ignoradas: list = field(default_factory=list)         # VentaPlatillo, marcadas IGNORAR a mano


def emparejar_ventas_con_recetas(ventas: list[VentaPlatillo], recetas: dict, mapeo_manual: dict | None = None) -> MatchResultado:
    """
    Empareja cada clave vendida en Wansoft con una clave de la tabla de
    recetas. Orden de reglas:

      0. Mapeo manual guardado desde el reporte (gana siempre sobre lo
         automatico, porque lo confirmo una persona).
      1. Match exacto (sin espacios, mayusculas).
      2. Si no hay match, se prueban los prefijos conocidos de canal de
         venta (DOM=domicilio, PLATAF=plataforma, REF=combo/promo) quitados.
      3. Sufijo 'SS' duplicado (p.ej. PAMSS) se recorta si la clave base
         (PAMS) existe y PAMSS no.
      4. Prefijo numerico tipo "2 ORDP" (promo de 2 unidades) se interpreta
         como multiplicador de cantidad sobre la clave base.

    Lo que no logra emparejarse NO se descarta silenciosamente: se regresa
    en ventas_sin_identificar para que se muestre en el reporte como
    pendiente, y esa cantidad no entra al calculo de consumo teorico.
    """
    recetas_norm = {_normalizar(k): k for k in recetas}
    mapeo_manual = mapeo_manual or {}
    resultado = MatchResultado()

    for venta in ventas:
        clave_norm = _normalizar(venta.clave)
        objetivo = None
        multiplicador = 1.0

        if clave_norm in mapeo_manual:
            decision = mapeo_manual[clave_norm]
            if decision == IGNORAR:
                resultado.ventas_ignoradas.append(venta)
                continue
            if decision in recetas:
                resultado.ventas_identificadas.append((venta.clave, decision, venta.cantidad))
                continue
            # si el mapeo guardado ya no es valido (receta renombrada), sigue
            # con las reglas automaticas en vez de fallar

        if clave_norm in recetas_norm:
            objetivo = recetas_norm[clave_norm]
        else:
            for prefijo in PREFIJOS_CONOCIDOS:
                prefijo_norm = _normalizar(prefijo)
                if clave_norm.startswith(prefijo_norm):
                    resto = clave_norm[len(prefijo_norm):]
                    if resto in recetas_norm:
                        objetivo = recetas_norm[resto]
                        break

        if objetivo is None and clave_norm.endswith("S"):
            # Wansoft agrega una 'S' extra a variantes "sin queso" (PAMS ->
            # PAMSS) que en la tabla de recetas de la familia son la MISMA
            # fila base (confirmado contra su calculo manual real).
            base = clave_norm[:-1]
            if base in recetas_norm:
                objetivo = recetas_norm[base]

        if objetivo is None:
            m = re.match(r"^(\d+)(.+)$", clave_norm)
            if m:
                posible_mult, resto = m.groups()
                if resto in recetas_norm:
                    objetivo = recetas_norm[resto]
                    multiplicador = float(posible_mult)

        if objetivo is not None:
            resultado.ventas_identificadas.append(
                (venta.clave, objetivo, venta.cantidad * multiplicador)
            )
        else:
            resultado.ventas_sin_identificar.append(venta)

    return resultado


# La receta guarda "tortillas" como PIEZAS por platillo, pero el inventario
# fisico las cuenta en KG. La familia ya usa este mismo factor (formula
# L50=L47/45 en su Excel: ~45 tortillas por paquete/kg) para convertir.
PIEZAS_TORTILLA_POR_KG = 45.0


def calcular_consumo_teorico(match: MatchResultado, recetas: dict) -> dict:
    """Suma, por insumo, cuanto se debio haber consumido segun las recetas."""
    consumo = {}
    for _clave_original, clave_receta, cantidad in match.ventas_identificadas:
        receta = recetas[clave_receta]["receta_por_unidad"]
        for insumo, cantidad_por_unidad in receta.items():
            consumo[insumo] = consumo.get(insumo, 0.0) + cantidad_por_unidad * cantidad
    if "tortillas" in consumo:
        consumo["tortillas"] = consumo["tortillas"] / PIEZAS_TORTILLA_POR_KG
    return consumo


# --- Lectura del "Formato de corte" (inventario fisico + dinero) ---------

def leer_inventario_formato_corte(path: str, hoja: str) -> list[ItemInventario]:
    """
    Lee la seccion de inventario (INICIAL / ENTRADA / FINAL) del formato de
    corte. Detecta las columnas por encabezado (fila con 'INICIAL', 'ENTRADA',
    'FINAL'), no por letra de columna fija, porque el layout puede variar.
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[hoja]

    col_producto = col_inicial = col_entrada = col_final = None
    fila_encabezado = None
    for row in ws.iter_rows(min_row=1, max_row=15):
        valores = {c.column: str(c.value).strip().upper() for c in row if isinstance(c.value, str)}
        if any("INICIAL" in v for v in valores.values()) and any("FINAL" in v for v in valores.values()):
            fila_encabezado = row[0].row
            for col, v in valores.items():
                if "INICIAL" in v:
                    col_inicial = col
                elif "ENTRADA" in v:
                    col_entrada = col
                elif "FINAL" in v:
                    col_final = col
            break
    if fila_encabezado is None or col_inicial is None or col_final is None:
        raise ValueError(f"No encontre encabezados INICIAL/ENTRADA/FINAL en la hoja '{hoja}'")

    # la columna de producto es la primera columna de texto a la izquierda de INICIAL
    col_producto = min(c for c in (col_inicial, col_entrada, col_final) if c) - 2
    col_unidad = col_producto + 1

    items = []
    fila = fila_encabezado + 1
    vacias_seguidas = 0
    while vacias_seguidas < 3 and fila < fila_encabezado + 60:
        producto = ws.cell(row=fila, column=col_producto).value
        if producto is None or not str(producto).strip():
            vacias_seguidas += 1
            fila += 1
            continue
        vacias_seguidas = 0
        unidad = ws.cell(row=fila, column=col_unidad).value if col_unidad else None
        inicial = ws.cell(row=fila, column=col_inicial).value
        entrada = ws.cell(row=fila, column=col_entrada).value if col_entrada else None
        final = ws.cell(row=fila, column=col_final).value
        items.append(
            ItemInventario(
                producto=str(producto).strip(),
                unidad=str(unidad).strip() if unidad else None,
                inicial=float(inicial) if isinstance(inicial, (int, float)) else None,
                entrada=float(entrada) if isinstance(entrada, (int, float)) else 0.0,
                final=float(final) if isinstance(final, (int, float)) else None,
            )
        )
        fila += 1

    return items


# --- Lectura del reporte de ventas de Wansoft -----------------------------

def leer_ventas_wansoft(path: str, hoja: str | None = None) -> list[VentaPlatillo]:
    """
    Lee 'Reporte Ventas Por Platillo Por Grupo' de Wansoft. Detecta las
    columnas 'Clave platillo', 'Platillo' y 'Cantidad' por encabezado.
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    hoja = hoja or wb.sheetnames[0]
    ws = wb[hoja]

    col_clave = col_platillo = col_cantidad = None
    fila_encabezado = None
    for row in ws.iter_rows(min_row=1, max_row=15):
        valores = {c.column: str(c.value).strip().upper() for c in row if isinstance(c.value, str)}
        if any("CLAVE" in v for v in valores.values()) and any("CANTIDAD" in v for v in valores.values()):
            fila_encabezado = row[0].row
            for col, v in valores.items():
                if "CLAVE" in v:
                    col_clave = col
                elif v.strip() == "PLATILLO":
                    col_platillo = col
                elif "CANTIDAD" in v:
                    col_cantidad = col
            break
    if fila_encabezado is None or col_clave is None or col_cantidad is None:
        raise ValueError("No encontre encabezados 'Clave platillo' / 'Cantidad' en el reporte de Wansoft")

    ventas = []
    for row in ws.iter_rows(min_row=fila_encabezado + 1, max_row=ws.max_row):
        clave = ws.cell(row=row[0].row, column=col_clave).value
        cantidad = ws.cell(row=row[0].row, column=col_cantidad).value
        if not clave or not isinstance(cantidad, (int, float)):
            continue
        nombre = ws.cell(row=row[0].row, column=col_platillo).value if col_platillo else ""
        ventas.append(VentaPlatillo(clave=str(clave).strip(), nombre=str(nombre or "").strip(), cantidad=float(cantidad)))

    return ventas


# Insumo de receta -> productos de inventario fisico que lo componen.
# (ej. "carne" cocinada sale de arrachera cocida + bistek cocido juntos,
# igual que en la tabla DIF.COMPARATIVO del Excel de la familia)
INSUMO_A_PRODUCTOS_INVENTARIO = {
    "carne": ["ARRACHERA COCIDA", "BISTEK COCIDO"],
    "pastor": ["TROMPO COCIDO"],
    "queso": ["QUESO CHIHUAHUA"],
    "tortillas": ["TORTILLA"],
    "telera": ["TELERA"],
}

# Productos que se comparan 1:1 directo contra la cantidad vendida (sin
# receta, sin factor) -- requieren mapear la clave de Wansoft directamente.
# Se deja vacio a proposito: mejor no reportar nada que adivinar la clave.
PRODUCTOS_DIRECTOS = {
    # "REFRESCO 600": ["REF600", "DOMREFRESCO"],
    # "AGUA CIEL": [...],
    # "POZOLE": [...],
}


# --- Reporte consolidado ---------------------------------------------------

def generar_reporte(path_formato_corte: str, hoja_corte: str, path_wansoft: str, hoja_wansoft: str | None = None) -> dict:
    recetas = _cargar_recetas()
    mapeo_manual = cargar_mapeo_manual()

    inventario = leer_inventario_formato_corte(path_formato_corte, hoja_corte)
    ventas = leer_ventas_wansoft(path_wansoft, hoja_wansoft)
    match = emparejar_ventas_con_recetas(ventas, recetas, mapeo_manual)
    consumo_teorico = calcular_consumo_teorico(match, recetas)

    inventario_por_producto = {_normalizar(i.producto): i for i in inventario}

    comparativo = []
    for insumo, productos in INSUMO_A_PRODUCTOS_INVENTARIO.items():
        items = [inventario_por_producto.get(_normalizar(p)) for p in productos]
        items = [i for i in items if i is not None]
        faltantes = [p for p in productos if _normalizar(p) not in inventario_por_producto]

        teorico = consumo_teorico.get(insumo)
        if not items or any(i.consumo_real is None for i in items):
            real = None
        else:
            real = sum(i.consumo_real for i in items)

        if real is None or teorico is None:
            diferencia = None
            alerta = None
        else:
            diferencia = round(teorico - real, 3)
            tolerancia = TOLERANCIAS_DEFAULT.get(insumo, 0.5)
            alerta = abs(diferencia) > tolerancia

        comparativo.append({
            "insumo": insumo,
            "productos_inventario": productos,
            "consumo_real": round(real, 3) if real is not None else "no disponible",
            "consumo_teorico": round(teorico, 3) if teorico is not None else "no disponible",
            "diferencia": diferencia if diferencia is not None else "no disponible",
            "alerta": alerta,
            "nota": f"falta en inventario: {', '.join(faltantes)}" if faltantes else None,
        })

    inventario_crudo = [
        {
            "producto": item.producto,
            "unidad": item.unidad,
            "inicial": item.inicial,
            "entrada": item.entrada,
            "final": item.final,
            "consumo_real": round(item.consumo_real, 3) if item.consumo_real is not None else "no disponible",
        }
        for item in inventario
    ]

    pendientes = [
        {"clave": v.clave, "nombre": v.nombre, "cantidad": v.cantidad}
        for v in match.ventas_sin_identificar
    ]

    total_vendido = sum(v.cantidad for v in ventas)
    total_identificado = sum(c for _, _, c in match.ventas_identificadas)
    pct_identificado = round(100 * total_identificado / total_vendido, 1) if total_vendido else 0.0

    advertencia = None
    if pct_identificado < 80:
        advertencia = (
            f"Solo se pudo identificar el {pct_identificado}% de las unidades vendidas ese dia "
            "contra la tabla de recetas. El consumo TEORICO de este reporte es un minimo, no el "
            "real -- probablemente mas bajo de lo que deberia. Las alertas de este dia hay que "
            "tomarlas con cautela hasta completar el diccionario de claves (ver pendientes)."
        )

    claves_receta_disponibles = [
        {"clave": clave, "nombre": info["nombre"]} for clave, info in sorted(recetas.items())
    ]

    return {
        "comparativo": comparativo,
        "inventario_crudo": inventario_crudo,
        "platillos_no_identificados": pendientes,
        "platillos_ignorados": [
            {"clave": v.clave, "nombre": v.nombre, "cantidad": v.cantidad}
            for v in match.ventas_ignoradas
        ],
        "recetas_disponibles": claves_receta_disponibles,
        "total_platillos_vendidos": total_vendido,
        "total_identificado": total_identificado,
        "total_sin_identificar": sum(v.cantidad for v in match.ventas_sin_identificar),
        "pct_identificado": pct_identificado,
        "advertencia_confiabilidad": advertencia,
    }
