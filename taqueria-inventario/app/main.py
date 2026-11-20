"""
App web sencilla: arrastras el "Formato de corte" (excel que manda la
sucursal cada noche) y el reporte de ventas de Wansoft, y te regresa el
reporte de conciliacion de inventario del dia.
"""
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

_logger = logging.getLogger("uvicorn.error")

from . import asesor_compras, caja, colchon, operacion, compras, dinero, exportar, historial, historial_dinero, pareto, reconciliacion, respaldo, resumen_ia, usuarios

STATIC_DIR = Path(__file__).parent / "static"

# Acceso maestro: usuario/contraseña de variables de entorno (nunca escritos
# en el código). Siempre funciona, con perfil de dueño, aunque se borren o
# desactiven todos los usuarios del panel -- así nadie se queda fuera.
# Además, cada persona puede tener su propio usuario con su perfil
# (ver usuarios.py). Sin acceso maestro configurado y sin usuarios creados,
# la app queda abierta (útil para correrla local en tu compu).
_APP_USUARIO = os.environ.get("APP_USUARIO")
_APP_PASSWORD = os.environ.get("APP_PASSWORD")
_seguridad = HTTPBasic(auto_error=False)


def requiere_acceso(request: Request, credenciales: HTTPBasicCredentials | None = Depends(_seguridad)):
    maestro_configurado = bool(_APP_USUARIO and _APP_PASSWORD)
    usuario = None
    if credenciales is not None:
        if (maestro_configurado
                and secrets.compare_digest(credenciales.username, _APP_USUARIO)
                and secrets.compare_digest(credenciales.password, _APP_PASSWORD)):
            usuario = {"usuario": _APP_USUARIO, "nombre": "Acceso maestro", "rol": "dueno", "sucursal": "", "maestro": True}
        else:
            usuario = usuarios.verificar(credenciales.username, credenciales.password)
    if usuario is None:
        if not maestro_configurado and not usuarios.hay_usuarios():
            usuario = {"usuario": "local", "nombre": "Uso local", "rol": "dueno", "sucursal": "", "maestro": True}
        else:
            raise HTTPException(status_code=401, detail="Acceso no autorizado", headers={"WWW-Authenticate": "Basic"})
    if not usuarios.permitido(usuario["rol"], request.method, request.url.path):
        raise HTTPException(status_code=403, detail="Tu perfil no tiene acceso a esta sección.")
    request.state.usuario = usuario


app = FastAPI(title="Conciliacion de inventario - Taqueria", dependencies=[Depends(requiere_acceso)])


@app.exception_handler(Exception)
async def manejar_error_inesperado(request: Request, exc: Exception):
    """Si algo truena sin que lo hayamos previsto, esto evita que el
    navegador reciba una pagina de error en HTML (que no se puede leer como
    JSON y confunde con un mensaje tipo "Unexpected token") -- en vez de
    eso regresa el mensaje real del error, para poder diagnosticarlo."""
    _logger.exception("Error inesperado en %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Error inesperado del servidor ({type(exc).__name__}): {exc}"},
    )


def _detectar_metadatos_wansoft(path_wansoft: str) -> dict:
    """
    Lee del propio reporte de Wansoft (sin que la usuaria tenga que
    tecleerlo):
      - la fecha completa ('Reporte del: 2026-09-05 al 2026-09-05')
      - el nombre de la sucursal ('Sucursal: T-Grill Sucursal Santa Catarina')
    """
    import openpyxl

    wb = openpyxl.load_workbook(path_wansoft, data_only=True)
    ws = wb[wb.sheetnames[0]]
    fecha_completa = dia = sucursal = None
    for row in ws.iter_rows(min_row=1, max_row=10):
        for cell in row:
            if not isinstance(cell.value, str):
                continue
            texto = cell.value.strip()
            if fecha_completa is None and "reporte del" in texto.lower():
                m = re.search(r"(\d{4}-\d{2}-\d{2})", texto)
                if m:
                    fecha_completa = m.group(1)
                    dia = fecha_completa.split("-")[2]
            if sucursal is None and texto.lower().startswith("sucursal:"):
                sucursal = texto.split(":", 1)[1].strip()
    return {"fecha": fecha_completa, "dia": dia, "sucursal": sucursal}


def _resolver_fecha_dia_sucursal(path_wansoft: str, fecha_elegida: str | None) -> dict:
    """Decide que dia/hoja usar. Si la usuaria elige una fecha a mano (nuevo
    selector), esa manda -- pero si el propio Wansoft trae una fecha
    distinta, se avisa en vez de proceder callado (podria ser que subio el
    archivo de otro dia por error)."""
    meta = _detectar_metadatos_wansoft(path_wansoft)
    if meta["sucursal"] is None:
        raise HTTPException(
            status_code=400,
            detail="No pude encontrar la sucursal ('Sucursal: ...') en el archivo que subiste como Wansoft. "
            "Verifica que sea el reporte 'Ventas Por Platillo Por Grupo' que exporta Wansoft "
            "(no otro archivo, como el control diario que se lleva aparte).",
        )

    if fecha_elegida:
        dia = fecha_elegida.split("-")[-1]
        if meta["dia"] and meta["dia"] != dia:
            raise HTTPException(
                status_code=400,
                detail=f"Elegiste el dia {dia}, pero el reporte de Wansoft dice que es del dia {meta['dia']}. "
                "Verifica que subiste el Wansoft del mismo dia que elegiste.",
            )
        return {"dia": dia, "fecha": fecha_elegida, "sucursal": meta["sucursal"], "origen": "La fecha que elegiste"}

    if meta["dia"] is None:
        raise HTTPException(
            status_code=400,
            detail="No pude encontrar la fecha ('Reporte del: ...') en el archivo de Wansoft. "
            "Verifica que sea el reporte de Ventas Por Platillo Por Grupo sin editar, o elige la fecha a mano.",
        )
    return {"dia": meta["dia"], "fecha": meta["fecha"], "sucursal": meta["sucursal"], "origen": "El reporte de Wansoft"}


def _elegir_hoja(path_formato_corte: str, dia: str, origen_fecha: str = "la fecha indicada") -> str:
    import openpyxl

    wb = openpyxl.load_workbook(path_formato_corte, read_only=True)
    for candidato in (dia, dia.lstrip("0") or "0", dia.zfill(2)):
        if candidato in wb.sheetnames:
            return candidato
    raise HTTPException(
        status_code=400,
        detail=f"{origen_fecha} es del dia {dia}, pero el formato de corte no tiene una hoja para ese dia. "
        "Verifica que subiste el 'Formato de corte' correcto (no el 'Formato de efectivo').",
    )


@app.post("/api/reconciliar")
async def reconciliar(
    formato_corte: UploadFile = File(...),
    wansoft: UploadFile = File(...),
    fecha: str | None = Form(None),
):
    with tempfile.TemporaryDirectory() as tmp:
        path_corte = Path(tmp) / "formato_corte.xlsx"
        path_wansoft = Path(tmp) / "wansoft.xlsx"
        path_corte.write_bytes(await formato_corte.read())
        path_wansoft.write_bytes(await wansoft.read())

        info = _resolver_fecha_dia_sucursal(str(path_wansoft), fecha)
        hoja = _elegir_hoja(str(path_corte), info["dia"], origen_fecha=info["origen"])

        try:
            reporte = reconciliacion.generar_reporte(
                path_formato_corte=str(path_corte),
                hoja_corte=hoja,
                path_wansoft=str(path_wansoft),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        reporte["dia_detectado"] = info["dia"]
        reporte["fecha"] = info["fecha"]
        reporte["sucursal"] = info["sucursal"]
        historial.guardar_reporte(info["fecha"], info["sucursal"], reporte)
        return reporte


@app.get("/api/historial")
async def ver_historial(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None):
    return historial.listar_historial(sucursal=sucursal, desde=desde, hasta=hasta)


@app.delete("/api/historial/{fecha}/{sucursal}")
async def borrar_reporte_historial(fecha: str, sucursal: str):
    historial.eliminar_reporte(fecha, sucursal)
    return {"ok": True}


@app.get("/api/dashboard")
async def ver_dashboard(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None):
    return historial.dashboard(sucursal=sucursal, desde=desde, hasta=hasta)


@app.get("/api/mermas")
async def ver_mermas(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None, insumo: str | None = None):
    return historial.mermas_detalle(sucursal=sucursal, desde=desde, hasta=hasta, insumo=insumo)


@app.get("/api/resumen-ia/disponible")
async def resumen_ia_disponible():
    return {"disponible": resumen_ia.disponible()}


@app.get("/api/resumen-ia")
async def ver_resumen_ia(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None):
    datos = historial.dashboard(sucursal=sucursal, desde=desde, hasta=hasta)
    texto = resumen_ia.generar_resumen(datos)
    if texto is None:
        raise HTTPException(status_code=503, detail="El resumen con IA no está configurado (falta ANTHROPIC_API_KEY).")
    return {"resumen": texto}


@app.get("/api/pendientes-acumulados")
async def ver_pendientes_acumulados():
    """Platillos no identificados de todo el historial, sumados y ordenados
    por volumen -- para priorizar una sesión de afinar recetas."""
    return historial.pendientes_acumulados()


@app.get("/api/historial/{fecha}/{sucursal}")
async def ver_reporte_historial(fecha: str, sucursal: str):
    reporte = historial.obtener_reporte(fecha, sucursal)
    if reporte is None:
        raise HTTPException(status_code=404, detail="No hay reporte guardado para esa fecha y sucursal.")
    return reporte


@app.post("/api/exportar")
async def exportar_reporte(
    formato_corte: UploadFile = File(...),
    wansoft: UploadFile = File(...),
    fecha: str | None = Form(None),
):
    """Regenera el mismo reporte y lo entrega como Excel descargable."""
    with tempfile.TemporaryDirectory() as tmp:
        path_corte = Path(tmp) / "formato_corte.xlsx"
        path_wansoft = Path(tmp) / "wansoft.xlsx"
        path_corte.write_bytes(await formato_corte.read())
        path_wansoft.write_bytes(await wansoft.read())

        info = _resolver_fecha_dia_sucursal(str(path_wansoft), fecha)
        hoja = _elegir_hoja(str(path_corte), info["dia"], origen_fecha=info["origen"])

        try:
            reporte = reconciliacion.generar_reporte(
                path_formato_corte=str(path_corte), hoja_corte=hoja, path_wansoft=str(path_wansoft),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        reporte["dia_detectado"] = info["dia"]
        reporte["fecha"] = info["fecha"]
        reporte["sucursal"] = info["sucursal"]

    buffer = exportar.generar_excel_reporte(reporte)
    nombre_archivo = f"conciliacion_{info['fecha']}_{info['sucursal'].replace(' ', '-')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )


@app.post("/api/mapeo")
async def guardar_mapeo(payload: dict = Body(...)):
    """Guarda la correccion manual de una clave no identificada: a que clave
    de receta corresponde, o IGNORAR si no debe entrar al calculo (ej.
    refrescos, que se comparan distinto)."""
    clave = payload.get("clave")
    receta_o_ignorar = payload.get("receta_o_ignorar")
    if not clave or not receta_o_ignorar:
        raise HTTPException(status_code=400, detail="Falta 'clave' o 'receta_o_ignorar'.")
    reconciliacion.guardar_mapeo_manual(clave, receta_o_ignorar)
    return {"ok": True}


@app.get("/api/mapeo")
async def listar_mapeo():
    """Todos los mapeos manuales guardados, para poder revisarlos y
    corregirlos (ej. algo marcado como 'ignorar' que ahora si se quiere
    contemplar)."""
    return reconciliacion.listar_mapeo_manual()


@app.delete("/api/mapeo/{clave}")
async def eliminar_mapeo(clave: str):
    reconciliacion.eliminar_mapeo_manual(clave)
    return {"ok": True}


@app.get("/api/recetas")
async def listar_recetas():
    return reconciliacion.listar_recetas()


@app.post("/api/recetas")
async def guardar_receta(payload: dict = Body(...)):
    clave = payload.get("clave")
    nombre = payload.get("nombre", "")
    receta_por_unidad = payload.get("receta_por_unidad", {})
    if not clave:
        raise HTTPException(status_code=400, detail="Falta 'clave'.")
    try:
        reconciliacion.guardar_receta(clave, nombre, receta_por_unidad)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/recetas/{clave}")
async def eliminar_receta(clave: str):
    reconciliacion.eliminar_receta(clave)
    return {"ok": True}


def _leer_fecha_hoja_corte(path_corte: str, hoja: str) -> str | None:
    return dinero.leer_fecha_del_corte(path_corte, hoja)


@app.post("/api/dinero/reconciliar")
async def reconciliar_dinero(
    formato_corte: UploadFile = File(...),
    sucursal: str = Form(...),
    fecha: str = Form(...),
):
    """A diferencia del inventario, aqui solo se sube el Formato de corte
    (no hace falta Wansoft) -- pero ese archivo no trae el nombre de la
    sucursal, asi que se pide en el formulario. La fecha exacta se toma del
    propio archivo cuando esta disponible (mas confiable que lo que se
    escriba a mano)."""
    if not sucursal.strip():
        raise HTTPException(status_code=400, detail="Falta indicar la sucursal.")
    with tempfile.TemporaryDirectory() as tmp:
        path_corte = Path(tmp) / "formato_corte.xlsx"
        path_corte.write_bytes(await formato_corte.read())

        dia = fecha.split("-")[-1] if fecha else ""
        if not dia:
            raise HTTPException(status_code=400, detail="Falta indicar la fecha.")
        hoja = _elegir_hoja(str(path_corte), dia, origen_fecha="La fecha que indicaste")

        try:
            reporte = dinero.generar_reporte_dinero(str(path_corte), hoja)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        fecha_detectada = _leer_fecha_hoja_corte(str(path_corte), hoja) or fecha
        reporte["fecha"] = fecha_detectada
        reporte["sucursal"] = sucursal.strip()
        historial_dinero.guardar_reporte(fecha_detectada, sucursal.strip(), reporte)
        return reporte


@app.get("/api/dinero/historial")
async def ver_historial_dinero(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None):
    return historial_dinero.listar_historial(sucursal=sucursal, desde=desde, hasta=hasta)


@app.get("/api/dinero/historial/{fecha}/{sucursal}")
async def ver_reporte_historial_dinero(fecha: str, sucursal: str):
    reporte = historial_dinero.obtener_reporte(fecha, sucursal)
    if reporte is None:
        raise HTTPException(status_code=404, detail="No hay reporte de dinero guardado para esa fecha y sucursal.")
    return reporte


@app.delete("/api/dinero/historial/{fecha}/{sucursal}")
async def borrar_reporte_historial_dinero(fecha: str, sucursal: str):
    historial_dinero.eliminar_reporte(fecha, sucursal)
    return {"ok": True}


@app.get("/api/dinero/dashboard")
async def ver_dashboard_dinero(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None):
    return historial_dinero.dashboard(sucursal=sucursal, desde=desde, hasta=hasta)


@app.get("/api/dinero/pendientes-acumulados")
async def ver_pendientes_acumulados_dinero():
    return historial_dinero.pendientes_acumulados()


@app.get("/api/dinero/categorias")
async def listar_categorias_gasto():
    return dinero.listar_categorias()


@app.post("/api/dinero/categorias")
async def guardar_categoria_gasto(payload: dict = Body(...)):
    nombre = payload.get("nombre")
    grupo = payload.get("grupo", "General")
    if not nombre:
        raise HTTPException(status_code=400, detail="Falta 'nombre'.")
    try:
        dinero.guardar_categoria(nombre, grupo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/dinero/categorias/{nombre}")
async def eliminar_categoria_gasto(nombre: str):
    dinero.eliminar_categoria(nombre)
    return {"ok": True}


@app.post("/api/dinero/mapeo")
async def guardar_mapeo_gasto(payload: dict = Body(...)):
    """Asigna manualmente un concepto de gasto en texto libre (ej. 'SUPER')
    a una categoria fija, o IGNORAR si no debe contar (nunca se adivina)."""
    concepto = payload.get("concepto")
    categoria_o_ignorar = payload.get("categoria_o_ignorar")
    if not concepto or not categoria_o_ignorar:
        raise HTTPException(status_code=400, detail="Falta 'concepto' o 'categoria_o_ignorar'.")
    dinero.guardar_mapeo_gasto(concepto, categoria_o_ignorar)
    return {"ok": True}


@app.get("/api/dinero/colchon")
async def ver_colchon(sucursal: str, mes: str):
    """mes en formato 'YYYY-MM'."""
    return colchon.calcular_debo_tener(sucursal, mes)


@app.post("/api/dinero/colchon/inicial")
async def guardar_colchon_inicial(payload: dict = Body(...)):
    """Colchon inicial del mes (el sobrante real del mes anterior) -- se
    captura a mano, nunca se asume $0."""
    sucursal = payload.get("sucursal")
    mes = payload.get("mes")
    monto = payload.get("monto")
    if not sucursal or not mes or monto is None:
        raise HTTPException(status_code=400, detail="Falta 'sucursal', 'mes' o 'monto'.")
    colchon.guardar_colchon_inicial(sucursal, mes, float(monto))
    return {"ok": True}


@app.post("/api/dinero/colchon/salida")
async def agregar_salida_efectivo(payload: dict = Body(...)):
    """Registra una salida grande de efectivo (nomina, compra al mayoreo) --
    usa las mismas categorias que los gastos chicos del corte diario."""
    fecha = payload.get("fecha")
    sucursal = payload.get("sucursal")
    concepto = payload.get("concepto")
    categoria = payload.get("categoria")
    monto = payload.get("monto")
    if not fecha or not sucursal or not concepto or not categoria or monto is None:
        raise HTTPException(status_code=400, detail="Falta algun campo obligatorio.")
    nuevo_id = colchon.agregar_salida_efectivo(fecha, sucursal, concepto, categoria, float(monto))
    return {"ok": True, "id": nuevo_id}


@app.delete("/api/dinero/colchon/salida/{id_salida}")
async def eliminar_salida_efectivo(id_salida: int):
    colchon.eliminar_salida_efectivo(id_salida)
    return {"ok": True}


@app.get("/api/v2/pareto")
async def ver_pareto(desde: str | None = None, hasta: str | None = None, sucursal: str | None = None):
    """80/20 de insumos por costo y por cantidad, sobre las compras reales."""
    return pareto.analisis(desde=desde, hasta=hasta, sucursal=sucursal)


# --- Versión 2: caja, corte del día, cuentas y Quicken -------------------------

def _valor_error(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v2/menu")
async def v2_menu(vendibles: bool = False, insumos: bool = False):
    return caja.listar_menu(solo_vendibles=vendibles, con_insumos=insumos)


@app.get("/api/v2/categorias-menu")
async def v2_categorias_menu():
    return caja.CATEGORIAS


@app.post("/api/v2/menu/completo")
async def v2_guardar_platillo_completo(payload: dict = Body(...)):
    """Nombre, categoria, precio y lo que lleva el platillo, en un solo paso."""
    precio = payload.get("precio")
    clave = _valor_error(
        caja.guardar_platillo_con_insumos,
        payload.get("clave"), str(payload.get("nombre", "")).strip(), payload.get("categoria"),
        float(precio) if precio not in (None, "") else None,
        bool(payload.get("activo", True)), payload.get("insumos") or {},
    )
    return {"ok": True, "clave": clave}


@app.post("/api/v2/menu")
async def v2_guardar_platillo(payload: dict = Body(...)):
    precio = payload.get("precio")
    _valor_error(
        caja.guardar_platillo,
        str(payload.get("clave", "")), str(payload.get("nombre", "")),
        float(precio) if precio not in (None, "") else None,
        payload.get("categoria"), payload.get("receta"), bool(payload.get("activo", True)),
    )
    return {"ok": True}


@app.delete("/api/v2/menu/{clave}")
async def v2_eliminar_platillo(clave: str):
    caja.eliminar_platillo(clave)
    return {"ok": True}


@app.get("/api/v2/formas-pago")
async def v2_formas_pago():
    return caja.listar_formas_pago()


@app.post("/api/v2/formas-pago")
async def v2_guardar_comision(payload: dict = Body(...)):
    pct = payload.get("comision_pct")
    _valor_error(caja.guardar_comision, str(payload.get("forma_pago", "")), float(pct) if pct not in (None, "") else None)
    return {"ok": True}


@app.post("/api/v2/ventas")
async def v2_registrar_venta(request: Request, payload: dict = Body(...)):
    recibido = payload.get("recibido")
    # Un usuario ligado a una sucursal siempre cobra en la suya.
    sucursal = request.state.usuario.get("sucursal") or str(payload.get("sucursal", ""))
    return _valor_error(
        caja.registrar_venta,
        str(payload.get("fecha", "")), str(payload.get("hora", "")), sucursal,
        str(payload.get("forma_pago", "")), payload.get("items") or [],
        float(recibido) if recibido not in (None, "") else None,
        payload.get("id_local"),
    )


@app.get("/api/v2/ventas")
async def v2_listar_ventas(fecha: str, sucursal: str | None = None):
    return caja.listar_ventas(fecha, sucursal or None)


@app.post("/api/v2/ventas/{venta_id}/cancelar")
async def v2_cancelar_venta(venta_id: int):
    caja.cancelar_venta(venta_id)
    return {"ok": True}


@app.get("/api/v2/sucursales")
async def v2_sucursales():
    return caja.sucursales()


@app.get("/api/v2/corte")
async def v2_corte(fecha: str, sucursal: str):
    return caja.corte_del_dia(fecha, sucursal)


@app.post("/api/v2/corte")
async def v2_guardar_conteo(payload: dict = Body(...)):
    contado = payload.get("efectivo_contado")
    if not payload.get("fecha") or not payload.get("sucursal") or contado in (None, ""):
        raise HTTPException(status_code=400, detail="Falta fecha, sucursal o efectivo contado.")
    caja.guardar_conteo_efectivo(payload["fecha"], payload["sucursal"], float(contado), str(payload.get("notas") or ""))
    return caja.corte_del_dia(payload["fecha"], payload["sucursal"])


@app.post("/api/v2/inventario")
async def v2_inventario(formato_corte: UploadFile = File(...), fecha: str = Form(...), sucursal: str = Form(...)):
    """Consumo teorico con las ventas de la caja propia vs. inventario fisico
    del formato de corte. No se guarda en el historial: durante el piloto el
    reporte oficial sigue siendo el de Wansoft (Versión 1)."""
    ventas = caja.ventas_para_inventario(fecha, sucursal)
    if not ventas:
        raise HTTPException(status_code=400, detail="No hay ventas de la caja ese día en esa sucursal.")
    with tempfile.TemporaryDirectory() as tmp:
        path_corte = Path(tmp) / "formato_corte.xlsx"
        path_corte.write_bytes(await formato_corte.read())
        hoja = _elegir_hoja(str(path_corte), fecha.split("-")[-1], origen_fecha="La fecha del corte")
        try:
            reporte = reconciliacion.generar_reporte(path_formato_corte=str(path_corte), hoja_corte=hoja, ventas=ventas)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    reporte["fecha"] = fecha
    reporte["sucursal"] = sucursal
    return reporte


@app.get("/api/v2/inventario-dia")
async def v2_inventario_dia(request: Request, fecha: str, sucursal: str):
    sucursal = request.state.usuario.get("sucursal") or sucursal
    conteo = operacion.conteo_del_dia(fecha, sucursal)
    comp = operacion.comparativo_del_dia(fecha, sucursal)
    conteo["comparativo"] = comp["comparativo"] if comp else None
    conteo["pendientes"] = comp["platillos_no_identificados"] if comp else []
    return conteo


@app.post("/api/v2/inventario-dia")
async def v2_guardar_inventario_dia(request: Request, payload: dict = Body(...)):
    sucursal = request.state.usuario.get("sucursal") or str(payload.get("sucursal", "")).strip()
    fecha = str(payload.get("fecha", ""))
    if not fecha or not sucursal:
        raise HTTPException(status_code=400, detail="Falta fecha o sucursal.")
    _valor_error(operacion.guardar_conteo, fecha, sucursal, payload.get("productos") or [])
    return await v2_inventario_dia(request, fecha, sucursal)


@app.get("/api/v2/resumen-dia")
async def v2_resumen_dia(fecha: str):
    return operacion.resumen_del_dia(fecha)


@app.get("/api/v2/saldos")
async def v2_saldos(desde: str, hasta: str, sucursal: str | None = None):
    return caja.saldos_estimados(desde, hasta, sucursal or None)


@app.get("/api/v2/quicken.csv")
async def v2_quicken_csv(cuenta: str, desde: str, hasta: str, sucursal: str | None = None):
    import io
    contenido = caja.csv_quicken(cuenta, desde, hasta, sucursal or None)
    nombre = f"quicken_{cuenta.replace(' ', '-')}_{desde}_a_{hasta}.csv"
    return StreamingResponse(
        io.BytesIO(contenido.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# --- usuarios y permisos -------------------------------------------------------

@app.get("/api/yo")
async def quien_soy(request: Request):
    return {**request.state.usuario, "roles": usuarios.ROLES}


@app.get("/api/usuarios")
async def listar_usuarios():
    return usuarios.listar()


@app.post("/api/usuarios")
async def crear_usuario(payload: dict = Body(...)):
    _valor_error(usuarios.crear, str(payload.get("usuario", "")), str(payload.get("nombre", "")),
                 str(payload.get("rol", "")), payload.get("sucursal"), str(payload.get("password", "")))
    return {"ok": True}


def _no_a_ti_mismo(request: Request, usuario: str, accion: str):
    if request.state.usuario.get("usuario") == usuario and not request.state.usuario.get("maestro"):
        raise HTTPException(status_code=400, detail=f"No puedes {accion} tu propio usuario.")


@app.post("/api/usuarios/{usuario}")
async def actualizar_usuario(request: Request, usuario: str, payload: dict = Body(...)):
    rol = str(payload.get("rol", ""))
    activo = bool(payload.get("activo", True))
    if not activo or rol not in ("dueno", "administracion"):
        _no_a_ti_mismo(request, usuario, "desactivar o quitarle permisos a")
    _valor_error(usuarios.actualizar, usuario, str(payload.get("nombre", "")), rol, payload.get("sucursal"), activo)
    return {"ok": True}


@app.post("/api/usuarios/{usuario}/password")
async def cambiar_password_usuario(usuario: str, payload: dict = Body(...)):
    _valor_error(usuarios.cambiar_password, usuario, str(payload.get("password", "")))
    return {"ok": True}


@app.delete("/api/usuarios/{usuario}")
async def eliminar_usuario(request: Request, usuario: str):
    _no_a_ti_mismo(request, usuario, "borrar")
    usuarios.eliminar(usuario)
    return {"ok": True}


@app.get("/api/v2/respaldo.xlsx")
async def descargar_respaldo():
    """Toda la base de datos en un Excel (una hoja por tabla), para tenerla
    respaldada fuera del servidor."""
    import io
    from datetime import date
    contenido, _resumen = respaldo.generar_excel()
    return StreamingResponse(
        io.BytesIO(contenido),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="respaldo_tgrill_{date.today().isoformat()}.xlsx"'},
    )


@app.get("/api/compras")
async def listar_compras(
    ingrediente: str | None = None, proveedor: str | None = None,
    desde: str | None = None, hasta: str | None = None, sucursal: str | None = None,
):
    return compras.listar_compras(ingrediente=ingrediente, proveedor=proveedor, desde=desde, hasta=hasta, sucursal=sucursal)


@app.post("/api/compras")
async def agregar_compra(payload: dict = Body(...)):
    fecha = payload.get("fecha")
    ingrediente = payload.get("ingrediente")
    proveedor = payload.get("proveedor")
    cantidad = payload.get("cantidad")
    unidad = payload.get("unidad")
    precio_total = payload.get("precio_total")
    notas = payload.get("notas", "")
    sucursal = payload.get("sucursal")
    if not fecha or not ingrediente or not proveedor or precio_total is None:
        raise HTTPException(status_code=400, detail="Falta fecha, ingrediente, proveedor o precio.")
    try:
        nuevo_id = compras.agregar_compra(
            fecha, ingrediente, proveedor,
            float(cantidad) if cantidad not in (None, "") else None,
            unidad or None, float(precio_total), notas, sucursal or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": nuevo_id}


@app.delete("/api/compras/{id_compra}")
async def eliminar_compra(id_compra: int):
    compras.eliminar_compra(id_compra)
    return {"ok": True}


@app.get("/api/compras/comparar")
async def comparar_precio_compra(ingrediente: str, precio_total: float, cantidad: float | None = None, unidad: str | None = None):
    return compras.comparar_precio(ingrediente, cantidad, unidad, precio_total)


@app.get("/api/compras/sugerencias-proveedor")
async def sugerencias_proveedor(proveedor: str):
    """"Voy a comprar a X" -- que ingredientes ya se han comprado ahi antes
    y si conviene aprovechar o mejor comprarlos en otro proveedor."""
    return compras.sugerir_por_proveedor(proveedor)


@app.get("/api/compras/asesor/disponible")
async def asesor_disponible():
    return {"disponible": asesor_compras.disponible()}


@app.post("/api/compras/asesor")
async def preguntar_asesor(payload: dict = Body(...)):
    pregunta = payload.get("pregunta")
    ingrediente = payload.get("ingrediente")
    precio_total = payload.get("precio_total")
    cantidad = payload.get("cantidad")
    unidad = payload.get("unidad")
    if not pregunta or not ingrediente or precio_total is None:
        raise HTTPException(status_code=400, detail="Falta la pregunta, el ingrediente o el precio.")
    respuesta = asesor_compras.preguntar(pregunta, ingrediente, cantidad, unidad, float(precio_total))
    if respuesta is None:
        raise HTTPException(status_code=503, detail="El asesor con IA no está configurado (falta ANTHROPIC_API_KEY).")
    return {"respuesta": respuesta}


_SERVICE_WORKER = """
// Guarda la página y sus archivos en el navegador para que la caja abra
// aunque se caiga el internet. Siempre intenta primero la red (así cada
// deploy se ve de inmediato) y solo usa lo guardado si no hay conexión.
const CACHE = 'tgrill-v1';
self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(['/', '/static/logo.jpg'])).catch(() => {}));
});
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (!(e.request.mode === 'navigate' || url.pathname.startsWith('/static/'))) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copia = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copia)); }
      return r;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
"""


@app.get("/sw.js")
async def service_worker():
    from fastapi.responses import Response
    return Response(_SERVICE_WORKER, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
