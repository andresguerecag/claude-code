"""
Resumen en lenguaje natural del periodo filtrado del dashboard, usando la
API de Claude. Se genera solo cuando el usuario lo pide (boton), nunca
automatico, para controlar el costo -- cada resumen cuesta centavos de
dolar, pero no tiene caso gastarlo en cada cambio de filtro.

Si no hay ANTHROPIC_API_KEY configurada, la funcion regresa None y la app
simplemente no ofrece el boton -- no truena.
"""
from __future__ import annotations

import json
import os

_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SYSTEM = """Eres un asistente que ayuda a los dueños de una taquería (T-Grill) a \
entender, en español sencillo y sin jerga técnica, los datos de conciliación de \
inventario de su negocio. Te doy un resumen en JSON de un periodo (ventas por día \
y sucursal, alertas de merma, y consumo real vs. teórico por insumo).

Escribe un resumen corto (máximo 4-5 líneas), en tono directo y práctico, como si \
le hablaras a la dueña del negocio. Destaca:
- Lo más importante primero (una alerta grande pesa más que muchas normales).
- Si hay un patrón por sucursal o por insumo, dilo explícitamente.
- Si los datos son insuficientes o el periodo está vacío, dilo, no inventes.

NUNCA inventes números que no estén en el JSON. Si algo no se puede concluir con \
los datos dados, dilo en vez de adivinar."""


def disponible() -> bool:
    return bool(_API_KEY)


def generar_resumen(dashboard_data: dict) -> str | None:
    if not _API_KEY:
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=_API_KEY)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=500,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(dashboard_data, ensure_ascii=False)}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    return None
