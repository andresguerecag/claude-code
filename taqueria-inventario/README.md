# Conciliación de inventario — Taquería

App chica que reemplaza el copiar-pegar-comparar manual entre:

- el **Formato de corte** (inventario físico inicial/entrada/final que llena
  la sucursal cada noche), y
- el reporte de **Wansoft** "Ventas Por Platillo Por Grupo".

Sube los dos archivos, y la app calcula sola cuánto se debió consumir de
carne, pastor, queso y tortillas según las recetas (ya extraídas del Excel
real de la familia) y lo compara contra el inventario físico, marcando
alertas cuando la diferencia pasa la merma normal.

## Cómo correrla localmente

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Abre `http://127.0.0.1:8000` y arrastra los dos excels.

## Cómo se calculan las recetas

`app/recetas.json` se generó una sola vez con:

```bash
python3 scripts/extraer_recetas.py "CONTROL_DIARIO_....xlsx" <hoja> > app/recetas.json
```

Lee las fórmulas del Excel de recetas de la familia (no números inventados).
Si el menú cambia, se vuelve a correr este script sobre un archivo
actualizado y se reemplaza `recetas.json`.

## Qué NO hace todavía (a propósito)

- No concilia dinero (efectivo, tarjetas, depósitos) — eso es una fase
  aparte, con archivos distintos.
- Refrescos, agua y pozole no pasan por este comparativo automático todavía
  (se comparan distinto en el Excel original: 1 a 1 contra inventario, no
  por receta) — están fuera del alcance de la v1 para no inventar un mapeo
  que no se validó.
- Cualquier platillo vendido cuya clave de Wansoft no se logra identificar
  contra la tabla de recetas **no se descarta silenciosamente**: aparece en
  el reporte bajo "Platillos no identificados" con su cantidad, para
  revisión manual.

## Pendientes conocidos (para ajustar con la familia)

Con el primer archivo real (5 de septiembre) solo ~38% de las unidades
vendidas se identificaron automáticamente contra la tabla de recetas. El
resto cae en estos grupos — hay que decidir cada uno:

1. **Variantes "sin queso" con sufijo `SS`** (`PAMSS`, `PAPSS`): se asumió
   que son la misma receta que la versión base (`PAMS`, `PAPS`), confirmado
   contra el cálculo manual de un día real. Revisar si aplica siempre.
2. **Combos/promos con prefijo `REF`** (`REF CAMPGDE`, `REF ALBS`): se
   interpretan como el platillo base. Pero en la propia tabla de recetas de
   la familia, `REF ALBS` aparece agrupado con `ALBCQ` (con queso), no con
   `ALBS` — falta confirmar caso por caso.
3. **Pedidos de plataforma sin talla** (`PLATAF CAMPECHANA`,
   `PLATAF PIRATA ARRACHE`): no se sabe con certeza a qué tamaño/versión del
   platillo corresponden.
4. **Promos con multiplicador** (`2 ORDP` = 2 órdenes de pastor): se asumió
   que el número al inicio es un multiplicador de cantidad. Validado en un
   caso real, pero conviene confirmar que aplica igual en otras promos.
5. **Nombres que no coinciden con ninguna clave de receta**: `TACOA`,
   `TACOB`, `TACOP` (taquitos sueltos), `GRINGGDE`, `TORTBIST`, `DOMALACQ`,
   `PIRCHB`, extras (guacamole, frijoles, aguacate, nopal, champiñones) —
   faltan agregar a la tabla de recetas o confirmar que quedan fuera a
   propósito.
6. **Refrescos/agua/pozole**: en el Excel original se comparan por conteo
   directo (no por receta), con una tabla aparte que se llena a mano
   parcialmente. Falta decidir cómo automatizar esa parte también.

La app ya muestra estos pendientes en cada reporte (sección "Platillos no
identificados"), así que se pueden ir resolviendo con el uso real en vez de
adivinar todos de una vez.
