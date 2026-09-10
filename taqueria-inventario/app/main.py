"""
App web sencilla: arrastras el "Formato de corte" (excel que manda la
sucursal cada noche) y el reporte de ventas de Wansoft, y te regresa el
reporte de conciliacion de inventario del dia.
"""
import os
import re
import secrets
import tempfile
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from . import exportar, historial, reconciliacion

STATIC_DIR = Path(__file__).parent / "static"

# Contraseña compartida para proteger la app cuando esté en internet. Se lee
# de variables de entorno (nunca escrita en el código) -- si no se configura
# ninguna, la app queda SIN contraseña (útil para correrla local en tu compu
# sin que te pida usuario/clave cada vez).
_APP_USUARIO = os.environ.get("APP_USUARIO")
_APP_PASSWORD = os.environ.get("APP_PASSWORD")
_seguridad = HTTPBasic(auto_error=False)


def requiere_acceso(credenciales: HTTPBasicCredentials | None = Depends(_seguridad)):
    if not _APP_USUARIO or not _APP_PASSWORD:
        return  # sin variables de entorno configuradas -> app abierta (uso local)
    ok_usuario = credenciales is not None and secrets.compare_digest(credenciales.username, _APP_USUARIO)
    ok_clave = credenciales is not None and secrets.compare_digest(credenciales.password, _APP_PASSWORD)
    if not (ok_usuario and ok_clave):
        raise HTTPException(status_code=401, detail="Acceso no autorizado", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Conciliacion de inventario - Taqueria", dependencies=[Depends(requiere_acceso)])


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


def _elegir_hoja(path_formato_corte: str, dia: str) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(path_formato_corte, read_only=True)
    for candidato in (dia, dia.lstrip("0") or "0", dia.zfill(2)):
        if candidato in wb.sheetnames:
            return candidato
    raise HTTPException(
        status_code=400,
        detail=f"El reporte de Wansoft es del dia {dia}, pero el formato de corte no tiene una hoja para ese dia.",
    )


@app.post("/api/reconciliar")
async def reconciliar(formato_corte: UploadFile = File(...), wansoft: UploadFile = File(...)):
    with tempfile.TemporaryDirectory() as tmp:
        path_corte = Path(tmp) / "formato_corte.xlsx"
        path_wansoft = Path(tmp) / "wansoft.xlsx"
        path_corte.write_bytes(await formato_corte.read())
        path_wansoft.write_bytes(await wansoft.read())

        meta = _detectar_metadatos_wansoft(str(path_wansoft))
        if meta["dia"] is None:
            raise HTTPException(
                status_code=400,
                detail="No pude encontrar la fecha ('Reporte del: ...') en el archivo de Wansoft. "
                "Verifica que sea el reporte de Ventas Por Platillo Por Grupo sin editar.",
            )
        if meta["sucursal"] is None:
            raise HTTPException(
                status_code=400,
                detail="No pude encontrar la sucursal ('Sucursal: ...') en el archivo de Wansoft.",
            )
        hoja = _elegir_hoja(str(path_corte), meta["dia"])

        try:
            reporte = reconciliacion.generar_reporte(
                path_formato_corte=str(path_corte),
                hoja_corte=hoja,
                path_wansoft=str(path_wansoft),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        reporte["dia_detectado"] = meta["dia"]
        reporte["fecha"] = meta["fecha"]
        reporte["sucursal"] = meta["sucursal"]
        historial.guardar_reporte(meta["fecha"], meta["sucursal"], reporte)
        return reporte


@app.get("/api/historial")
async def ver_historial():
    return historial.listar_historial()


@app.get("/api/dashboard")
async def ver_dashboard(sucursal: str | None = None, desde: str | None = None, hasta: str | None = None):
    return historial.dashboard(sucursal=sucursal, desde=desde, hasta=hasta)


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
async def exportar_reporte(formato_corte: UploadFile = File(...), wansoft: UploadFile = File(...)):
    """Regenera el mismo reporte y lo entrega como Excel descargable."""
    with tempfile.TemporaryDirectory() as tmp:
        path_corte = Path(tmp) / "formato_corte.xlsx"
        path_wansoft = Path(tmp) / "wansoft.xlsx"
        path_corte.write_bytes(await formato_corte.read())
        path_wansoft.write_bytes(await wansoft.read())

        meta = _detectar_metadatos_wansoft(str(path_wansoft))
        if meta["dia"] is None:
            raise HTTPException(status_code=400, detail="No pude encontrar la fecha en el archivo de Wansoft.")
        hoja = _elegir_hoja(str(path_corte), meta["dia"])

        try:
            reporte = reconciliacion.generar_reporte(
                path_formato_corte=str(path_corte), hoja_corte=hoja, path_wansoft=str(path_wansoft),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        reporte["dia_detectado"] = meta["dia"]
        reporte["fecha"] = meta["fecha"]
        reporte["sucursal"] = meta["sucursal"]

    buffer = exportar.generar_excel_reporte(reporte)
    nombre_archivo = f"conciliacion_{meta['fecha']}_{meta['sucursal'].replace(' ', '-')}.xlsx"
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


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
