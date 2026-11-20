"""
Usuarios, perfiles y permisos.

Perfiles (como se planteo en la presentacion):
  - dueno           -> todo, incluido administrar usuarios
  - administracion  -> todo, incluido administrar usuarios (la mama)
  - encargada       -> caja, corte del dia, inventario y Versión 1; no ve
                       cuentas/Quicken, respaldo, ni edita menu, comisiones
                       o usuarios
  - cajero          -> solo la caja (cobrar y ver las ventas del dia)

El usuario/contraseña de las variables de entorno APP_USUARIO/APP_PASSWORD
sigue funcionando siempre como acceso maestro con perfil de dueño, para que
nunca nadie se quede fuera aunque se borren o desactiven todos los usuarios.

Las contraseñas se guardan con PBKDF2 (sal por usuario), nunca en texto.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

from . import db

DB_PATH = Path(__file__).parent / "historial.db"
ROLES = {
    "dueno": "Dueño",
    "administracion": "Administración",
    "encargada": "Encargada de sucursal",
    "cajero": "Cajero",
}
ITERACIONES = 200_000

_DDL = """CREATE TABLE IF NOT EXISTS usuarios (
    usuario TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    rol TEXT NOT NULL,
    sucursal TEXT,
    hash TEXT NOT NULL,
    sal TEXT NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1
)"""

# Cada peticion del navegador trae usuario y contraseña (HTTP Basic). Para no
# recalcular PBKDF2 (lento a proposito) en cada una, se recuerda por unos
# minutos que esa combinacion ya fue verificada. Cualquier cambio a usuarios
# vacia esta memoria.
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_CACHE_SEGUNDOS = 300


def _conectar():
    if db.usando_postgres():
        con = db.conectar()
    else:
        con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(_DDL)
    con.commit()
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


def _ejecutar(consulta: str, params: tuple = ()) -> None:
    con = _conectar()
    cur = con.cursor()
    cur.execute(_sql(con, consulta), params)
    con.commit()
    con.close()
    _cache.clear()


def _hash(password: str, sal: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(sal), ITERACIONES).hex()


def _validar(usuario: str, nombre: str, rol: str) -> None:
    if not usuario or not usuario.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise ValueError("El usuario solo puede tener letras, números, punto, guion o guion bajo.")
    if not nombre.strip():
        raise ValueError("Falta el nombre.")
    if rol not in ROLES:
        raise ValueError("Perfil desconocido.")


def hay_usuarios() -> bool:
    return bool(_consultar("SELECT COUNT(*) FROM usuarios")[0][0])


def listar() -> list[dict]:
    filas = _consultar("SELECT usuario, nombre, rol, sucursal, activo FROM usuarios ORDER BY rol, nombre")
    return [{"usuario": u, "nombre": n, "rol": r, "rol_nombre": ROLES.get(r, r), "sucursal": s or "", "activo": bool(a)}
            for u, n, r, s, a in filas]


def crear(usuario: str, nombre: str, rol: str, sucursal: str | None, password: str) -> None:
    usuario = usuario.strip().lower()
    _validar(usuario, nombre, rol)
    if len(password) < 6:
        raise ValueError("La contraseña debe tener al menos 6 caracteres.")
    if _consultar("SELECT 1 FROM usuarios WHERE usuario = ?", (usuario,)):
        raise ValueError(f"Ya existe el usuario {usuario}.")
    sal = secrets.token_hex(16)
    _ejecutar("INSERT INTO usuarios (usuario, nombre, rol, sucursal, hash, sal, activo) VALUES (?, ?, ?, ?, ?, ?, 1)",
              (usuario, nombre.strip(), rol, (sucursal or "").strip() or None, _hash(password, sal), sal))


def actualizar(usuario: str, nombre: str, rol: str, sucursal: str | None, activo: bool) -> None:
    _validar(usuario, nombre, rol)
    _ejecutar("UPDATE usuarios SET nombre = ?, rol = ?, sucursal = ?, activo = ? WHERE usuario = ?",
              (nombre.strip(), rol, (sucursal or "").strip() or None, 1 if activo else 0, usuario))


def cambiar_password(usuario: str, password: str) -> None:
    if len(password) < 6:
        raise ValueError("La contraseña debe tener al menos 6 caracteres.")
    sal = secrets.token_hex(16)
    _ejecutar("UPDATE usuarios SET hash = ?, sal = ? WHERE usuario = ?", (_hash(password, sal), sal, usuario))


def eliminar(usuario: str) -> None:
    _ejecutar("DELETE FROM usuarios WHERE usuario = ?", (usuario,))


def verificar(usuario: str, password: str) -> dict | None:
    """El usuario si la contraseña es correcta y esta activo; si no, None."""
    usuario = (usuario or "").strip().lower()
    clave_cache = (usuario, hashlib.sha256(password.encode("utf-8")).hexdigest())
    guardado = _cache.get(clave_cache)
    if guardado and time.time() - guardado[0] < _CACHE_SEGUNDOS:
        return guardado[1]
    filas = _consultar("SELECT usuario, nombre, rol, sucursal, hash, sal, activo FROM usuarios WHERE usuario = ?", (usuario,))
    if not filas:
        return None
    u, nombre, rol, sucursal, h, sal, activo = filas[0]
    if not activo or not secrets.compare_digest(_hash(password, sal), h):
        return None
    datos = {"usuario": u, "nombre": nombre, "rol": rol, "sucursal": sucursal or "", "maestro": False}
    _cache[clave_cache] = (time.time(), datos)
    return datos


# --- permisos ------------------------------------------------------------------

def _es(path: str, *prefijos: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in prefijos)


def permitido(rol: str, metodo: str, path: str) -> bool:
    if rol in ("dueno", "administracion"):
        return True
    # lo que cualquiera con sesion necesita para cargar la pagina
    if path in ("/", "/favicon.ico", "/sw.js") or _es(path, "/static", "/api/yo"):
        return True
    if rol == "cajero":
        if metodo == "GET" and _es(path, "/api/v2/menu", "/api/v2/formas-pago", "/api/v2/sucursales", "/api/v2/ventas"):
            return True
        return metodo == "POST" and path == "/api/v2/ventas"
    if rol == "encargada":
        if _es(path, "/api/usuarios", "/api/v2/respaldo.xlsx", "/api/v2/quicken.csv", "/api/v2/saldos", "/api/v2/resumen-dia", "/api/v2/liquidaciones"):
            return False
        if metodo != "GET" and _es(path, "/api/v2/menu", "/api/v2/formas-pago"):
            return False
        return True
    return False
