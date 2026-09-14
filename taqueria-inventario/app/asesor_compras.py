"""
Asesor de compras: usa la API de Claude para responder "a este precio,
¿conviene comprarlo?", comparando contra el historial de compras guardado
en compras.py.

Igual que resumen_ia.py: bajo demanda (nunca automatico, para controlar el
costo) y solo disponible si hay ANTHROPIC_API_KEY configurada.
"""
from __future__ import annotations

import json
import os

from . import compras

_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SYSTEM = """Eres un asistente que ayuda a los dueños de una taquería (T-Grill) a decidir \
si conviene comprar un ingrediente a cierto precio (ej. "vi el aceite en oferta en Sam's, \
¿conviene comprarlo?"). Te doy la pregunta y el historial de compras pasadas de ese \
ingrediente (fecha, proveedor, cantidad, unidad, precio unitario).

Responde en español sencillo, corto y directo (máximo 5-6 líneas):
- Compara el precio de ahora contra el promedio/mínimo/máximo histórico.
- Di claramente si conviene comprar o no, y por qué, en un tono práctico.
- Si el historial viene vacío o muy corto, dilo explícitamente y dí que hace falta más \
historial para poder comparar con confianza -- NUNCA inventes un precio de referencia \
que no esté en los datos.
- Nunca inventes proveedores, fechas o precios que no estén en los datos que te doy."""


def disponible() -> bool:
    return bool(_API_KEY)


def preguntar(pregunta: str, ingrediente: str) -> str | None:
    if not _API_KEY:
        return None

    import anthropic

    historial = compras.historial_ingrediente(ingrediente)
    estadisticas = compras.estadisticas_ingrediente(ingrediente)
    contexto = {
        "pregunta": pregunta,
        "ingrediente_consultado": ingrediente,
        "estadisticas_historicas": estadisticas,
        "compras_pasadas": historial,
    }

    client = anthropic.Anthropic(api_key=_API_KEY)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=500,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(contexto, ensure_ascii=False)}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    return None
