"""
App web sencilla: arrastras el "Formato de corte" (excel que manda la
sucursal cada noche) y el reporte de ventas de Wansoft, y te regresa el
reporte de conciliacion de inventario del dia.
"""
import re
import tempfile
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import exportar, reconciliacion

app = FastAPI(title="Conciliacion de inventario - Taqueria")

STATIC_DIR = Path(__file__).parent / "static"


def _detectar_dia_del_reporte(path_wansoft: str) -> str | None:
    """Busca 'Reporte del: 2026-09-05 al 2026-09-05' en las primeras filas
    del reporte de Wansoft y regresa el dia del mes como texto ('05')."""
    import openpyxl

    wb = openpyxl.load_workbook(path_wansoft, data_only=True)
    ws = wb[wb.sheetnames[0]]
    for row in ws.iter_rows(min_row=1, max_row=10):
        for cell in row:
            if isinstance(cell.value, str) and "reporte del" in cell.value.lower():
                m = re.search(r"(\d{4})-(\d{2})-(\d{2})", cell.value)
                if m:
                    return m.group(3)
    return None


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

        dia = _detectar_dia_del_reporte(str(path_wansoft))
        if dia is None:
            raise HTTPException(
                status_code=400,
                detail="No pude encontrar la fecha ('Reporte del: ...') en el archivo de Wansoft. "
                "Verifica que sea el reporte de Ventas Por Platillo Por Grupo sin editar.",
            )
        hoja = _elegir_hoja(str(path_corte), dia)

        try:
            reporte = reconciliacion.generar_reporte(
                path_formato_corte=str(path_corte),
                hoja_corte=hoja,
                path_wansoft=str(path_wansoft),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        reporte["dia_detectado"] = dia
        return reporte


@app.post("/api/exportar")
async def exportar_reporte(formato_corte: UploadFile = File(...), wansoft: UploadFile = File(...)):
    """Regenera el mismo reporte y lo entrega como Excel descargable."""
    with tempfile.TemporaryDirectory() as tmp:
        path_corte = Path(tmp) / "formato_corte.xlsx"
        path_wansoft = Path(tmp) / "wansoft.xlsx"
        path_corte.write_bytes(await formato_corte.read())
        path_wansoft.write_bytes(await wansoft.read())

        dia = _detectar_dia_del_reporte(str(path_wansoft))
        if dia is None:
            raise HTTPException(status_code=400, detail="No pude encontrar la fecha en el archivo de Wansoft.")
        hoja = _elegir_hoja(str(path_corte), dia)

        try:
            reporte = reconciliacion.generar_reporte(
                path_formato_corte=str(path_corte), hoja_corte=hoja, path_wansoft=str(path_wansoft),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        reporte["dia_detectado"] = dia

    buffer = exportar.generar_excel_reporte(reporte)
    nombre_archivo = f"conciliacion_dia_{dia}.xlsx"
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


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
