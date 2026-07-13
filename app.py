"""SIGEMEP - Sistema de Consulta y Auditoría de Memorandos."""
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import secrets
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import (
    BASE_DIR,
    MAX_INTENTOS_LOGIN,
    MAX_UPLOAD_MB,
    PREVIEWS_DIR,
    SESSION_COOKIE_SECURE,
    SESSION_INACTIVITY_TIMEOUT_MINUTOS,
    SESSION_SECRET,
)
from services.audit_service import listar_auditoria, registrar_auditoria, registrar_busqueda
from services.auth_service import (
    generar_temp_password,
    hash_password,
    obtener_usuario_por_id,
    obtener_usuario_por_login,
    puede_registrar_rol,
    validar_username,
    validar_usuario_unico,
    verificar_password,
)
from services.db import get_config, get_db, init_db, rebuild_fts, set_config
from services.pdf_service import (
    carpeta_pdf_actual,
    carpeta_reservados_actual,
    extraer_fecha_hecho,
    imagen_pagina_con_marca,
    imagen_primera_hoja_con_marca,
    indexar_memorandos,
    renderizar_primera_hoja_base,
    ruta_absoluta_segura,
    ruta_absoluta_segura_reservados,
)
from services.search_service import buscar_memorandos
from services.memo_creator import generar_pdf_memorando, nombre_archivo_memorando

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sigemep")


class SessionTimeoutMiddleware(BaseHTTPMiddleware):
    """Expira la sesión por inactividad y asegura que exista un token CSRF."""

    async def dispatch(self, request: Request, call_next):
        if request.session.get("user_id"):
            now = datetime.now().timestamp()
            ultima_actividad = request.session.get("last_activity")
            limite = SESSION_INACTIVITY_TIMEOUT_MINUTOS * 60
            if ultima_actividad and (now - ultima_actividad) > limite:
                request.session.clear()
            else:
                request.session["last_activity"] = now
        if "csrf_token" not in request.session:
            request.session["csrf_token"] = secrets.token_hex(32)
        return await call_next(request)


async def verificar_csrf(request: Request, csrf_token: Optional[str] = Form(None)) -> None:
    """Dependencia para usar con Depends() en cada ruta POST/PUT/PATCH/DELETE.

    El token se acepta en el campo de formulario "csrf_token" (forms HTML
    normales, ya cubierto por el parámetro Form de esta misma función, que
    FastAPI fusiona con los demás Form()/File() de la ruta en una sola
    lectura del body) o en la cabecera "X-CSRF-Token" (llamadas fetch/XHR
    desde JS que no parten de un <form>).

    Nota: esto NO se implementa como middleware porque BaseHTTPMiddleware
    rompe la lectura posterior del body si se lo lee antes de llamar a
    call_next (el body ya fue drenado del receive() original de ASGI).
    """
    token_sesion = request.session.get("csrf_token")
    token_enviado = request.headers.get("x-csrf-token") or csrf_token
    if (
        not token_sesion
        or not token_enviado
        or not hmac.compare_digest(str(token_enviado), str(token_sesion))
    ):
        raise HTTPException(
            status_code=403,
            detail="Token CSRF inválido o ausente. Recargue la página e intente nuevamente.",
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Cabeceras HTTP de seguridad básicas para todas las respuestas."""

    _CSP = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    # CSP para el stream de PDF: permite embebido desde el mismo origen
    # (necesario para el iframe del visor /ver_pdf_completo)
    _CSP_PDF_STREAM = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        es_pdf_stream = request.url.path.startswith("/pdf_completo_stream/")
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if es_pdf_stream else "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = self._CSP_PDF_STREAM if es_pdf_stream else self._CSP
        return response


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="SIGEMEP", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# El orden de registro importa: Starlette ejecuta el último middleware añadido
# como el más externo. SessionMiddleware debe quedar más externo que
# SessionTimeoutMiddleware para que request.session ya exista cuando este la
# lea/modifique (la validación CSRF en sí se hace por ruta, vía Depends
# (verificar_csrf), no como middleware: ver comentario en verificar_csrf).
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SessionTimeoutMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
)

ROLES_CONSULTA_MEMORANDOS = frozenset({"ADMIN", "JEFE", "BRIGADA"})
ROLES_DESCARGA_PDF = frozenset({"ADMIN", "JEFE"})
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

INDEX_JOBS: dict[str, dict[str, Any]] = {}
INDEX_JOBS_LOCK = threading.Lock()

RESERVADOS_INDEX_JOBS: dict[str, dict[str, Any]] = {}
RESERVADOS_INDEX_JOBS_LOCK = threading.Lock()


@app.on_event("startup")
def _startup() -> None:
    init_db()


def client_ip(request: Request) -> str:
    xf = request.headers.get("x-forwarded-for")
    if xf:
        return xf.split(",")[0].strip()
    return request.client.host if request.client else ""


def ua(request: Request) -> str:
    return request.headers.get("user-agent", "")[:500]


def session_user_id(request: Request) -> Optional[int]:
    uid = request.session.get("user_id")
    return int(uid) if uid is not None else None


def require_login(request: Request) -> dict[str, Any]:
    uid = session_user_id(request)
    if not uid:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    with get_db() as conn:
        row = obtener_usuario_por_id(conn, uid)
    if not row:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    u = dict(row)
    if u["estado"] != "ACTIVO":
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return u


def require_password_ok(request: Request, user: dict[str, Any]) -> None:
    if user.get("debe_cambiar_password"):
        path = request.url.path
        if path not in ("/cambiar_password", "/logout") and not path.startswith("/static"):
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/cambiar_password"})


def require_roles(*roles: str):
    def dep(request: Request, user: Annotated[dict, Depends(require_login)]) -> dict[str, Any]:
        require_password_ok(request, user)
        if user["rol"] not in roles:
            with get_db() as conn:
                registrar_auditoria(
                    conn,
                    "ACCESO_DENEGADO",
                    usuario_id=user["id"],
                    detalle=f"Ruta: {request.url.path}",
                    ip=client_ip(request),
                    equipo=ua(request),
                    resultado="DENEGADO",
                )
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/acceso_denegado"})
        return user
    return dep


def require_reservados(request: Request, user: Annotated[dict, Depends(require_login)]) -> dict[str, Any]:
    require_password_ok(request, user)
    if user["rol"] != "ADMIN" and not user.get("permiso_reservados"):
        with get_db() as conn:
            registrar_auditoria(
                conn,
                "ACCESO_DENEGADO",
                usuario_id=user["id"],
                detalle=f"Ruta: {request.url.path}",
                ip=client_ip(request),
                equipo=ua(request),
                resultado="DENEGADO",
            )
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/acceso_denegado"})
    return user


def require_admin(request: Request, user: Annotated[dict[str, Any], Depends(require_login)]) -> dict[str, Any]:
    require_password_ok(request, user)
    if user["rol"] != "ADMIN":
        with get_db() as conn:
            registrar_auditoria(
                conn,
                "ACCESO_DENEGADO",
                usuario_id=user["id"],
                detalle=f"Intento de acceso administrativo: {request.url.path}",
                ip=client_ip(request),
                equipo=ua(request),
                resultado="DENEGADO",
            )
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/acceso_denegado"})
    return user


def _set_index_job(job_id: str, data: dict[str, Any]) -> None:
    with INDEX_JOBS_LOCK:
        job = INDEX_JOBS.setdefault(job_id, {})
        job.update(data)
        job["actualizado_en"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _get_index_job(job_id: str) -> dict[str, Any]:
    with INDEX_JOBS_LOCK:
        return dict(INDEX_JOBS.get(job_id, {}))


def _index_worker(job_id: str, admin_id: int, ip: str, equipo: str, force: bool) -> None:
    modo = "completa" if force else "rapida"
    accion_ini = "REINDEXACION_COMPLETA_INICIADA" if force else "INDEXACION_RAPIDA_INICIADA"
    accion_fin = "REINDEXACION_COMPLETA_FINALIZADA" if force else "INDEXACION_RAPIDA_FINALIZADA"
    try:
        _set_index_job(job_id, {
            "estado": "preparando", "modo": modo, "total": 0, "procesados": 0,
            "sin_cambios": 0, "nuevos": 0, "actualizados": 0,
            "no_encontrados": 0, "errores": 0, "archivo_actual": "",
            "mensaje": "Preparando indexación...",
        })
        with get_db() as conn:
            carpeta = str(carpeta_pdf_actual(conn))
            registrar_auditoria(conn, accion_ini, usuario_id=admin_id, detalle={"job_id": job_id, "carpeta": carpeta}, ip=ip, equipo=equipo, resultado="OK")

            def progress(data: dict[str, Any]) -> None:
                _set_index_job(job_id, data)

            stats = indexar_memorandos(conn, admin_id, progress_callback=progress, force=force)
            registrar_auditoria(conn, accion_fin, usuario_id=admin_id, detalle={"job_id": job_id, **stats}, ip=ip, equipo=equipo, resultado="OK")
        _set_index_job(job_id, {"estado": "finalizado", "mensaje": "Indexación finalizada."})
    except Exception as exc:
        logger.error("Error en indexación %s: %s\n%s", job_id, exc, traceback.format_exc())
        try:
            with get_db() as conn:
                registrar_auditoria(conn, "ERROR_INDEXACION_PDF", usuario_id=admin_id, detalle={"job_id": job_id, "error": str(exc)[:500]}, ip=ip, equipo=equipo, resultado="ERROR")
        except Exception:
            pass
        _set_index_job(job_id, {"estado": "error", "mensaje": str(exc)[:500]})


def _set_reservados_index_job(job_id: str, data: dict[str, Any]) -> None:
    with RESERVADOS_INDEX_JOBS_LOCK:
        job = RESERVADOS_INDEX_JOBS.setdefault(job_id, {})
        job.update(data)
        job["actualizado_en"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _get_reservados_index_job(job_id: str) -> dict[str, Any]:
    with RESERVADOS_INDEX_JOBS_LOCK:
        return dict(RESERVADOS_INDEX_JOBS.get(job_id, {}))


def _index_reservados_worker(job_id: str, admin_id: int, ip: str, equipo: str, force: bool) -> None:
    modo = "completa" if force else "rapida"
    accion_ini = "REINDEXACION_RESERVADOS_COMPLETA_INICIADA" if force else "INDEXACION_RESERVADOS_RAPIDA_INICIADA"
    accion_fin = "REINDEXACION_RESERVADOS_COMPLETA_FINALIZADA" if force else "INDEXACION_RESERVADOS_RAPIDA_FINALIZADA"
    try:
        _set_reservados_index_job(job_id, {
            "estado": "preparando", "modo": modo, "total": 0, "procesados": 0,
            "sin_cambios": 0, "nuevos": 0, "actualizados": 0,
            "no_encontrados": 0, "errores": 0, "archivo_actual": "",
            "mensaje": "Preparando indexación...",
        })
        with get_db() as conn:
            carpeta = carpeta_reservados_actual(conn)
            registrar_auditoria(conn, accion_ini, usuario_id=admin_id, detalle={"job_id": job_id, "carpeta": str(carpeta)}, ip=ip, equipo=equipo, resultado="OK")

            def progress(data: dict[str, Any]) -> None:
                _set_reservados_index_job(job_id, data)

            stats = indexar_memorandos(conn, admin_id, progress_callback=progress, force=force, tabla="reservados", carpeta=carpeta)
            registrar_auditoria(conn, accion_fin, usuario_id=admin_id, detalle={"job_id": job_id, **stats}, ip=ip, equipo=equipo, resultado="OK")
        _set_reservados_index_job(job_id, {"estado": "finalizado", "mensaje": "Indexación finalizada."})
    except Exception as exc:
        logger.error("Error en indexación de reservados %s: %s\n%s", job_id, exc, traceback.format_exc())
        try:
            with get_db() as conn:
                registrar_auditoria(conn, "ERROR_INDEXACION_RESERVADOS", usuario_id=admin_id, detalle={"job_id": job_id, "error": str(exc)[:500]}, ip=ip, equipo=equipo, resultado="ERROR")
        except Exception:
            pass
        _set_reservados_index_job(job_id, {"estado": "error", "mensaje": str(exc)[:500]})


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    loc = None
    if exc.headers:
        for hk, hv in exc.headers.items():
            if hk.lower() == "location":
                loc = hv
                break
    if loc and exc.status_code in (302, 303, 307):
        return RedirectResponse(loc, status_code=302)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exc(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return await http_exc_handler(request, exc)
    logger.error("%s\n%s", exc, traceback.format_exc())
    try:
        uid = session_user_id(request)
        with get_db() as conn:
            registrar_auditoria(conn, "ERROR_SISTEMA", usuario_id=uid, detalle=str(exc)[:500], ip=client_ip(request), equipo=ua(request), resultado="ERROR")
    except Exception:
        pass
    return templates.TemplateResponse("error.html", {"request": request, "mensaje": "Ocurrió un error en el sistema."}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    uid = session_user_id(request)
    if not uid:
        return RedirectResponse("/login", status_code=302)
    with get_db() as conn:
        row = obtener_usuario_por_id(conn, uid)
    if not row or row["estado"] != "ACTIVO":
        request.session.clear()
        return RedirectResponse("/login", status_code=302)
    u = dict(row)
    if u.get("debe_cambiar_password"):
        return RedirectResponse("/cambiar_password", status_code=302)
    if u["rol"] == "ADMIN":
        return RedirectResponse("/dashboard/admin", status_code=302)
    if u["rol"] == "JEFE":
        return RedirectResponse("/dashboard/jefe", status_code=302)
    return RedirectResponse("/dashboard/brigada", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if session_user_id(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, usuario: str = Form(...), password: str = Form(...), _csrf: None = Depends(verificar_csrf)):
    ip = client_ip(request)
    equipo = ua(request)
    with get_db() as conn:
        row = obtener_usuario_por_login(conn, usuario)
        if not row or not verificar_password(password, row["password_hash"]):
            mensaje = "Usuario o contraseña incorrectos."
            if row and row["estado"] == "ACTIVO":
                intentos = (row["intentos_fallidos"] or 0) + 1
                if intentos >= MAX_INTENTOS_LOGIN:
                    conn.execute(
                        "UPDATE usuarios SET estado = 'BLOQUEADO', intentos_fallidos = 0 WHERE id = ?",
                        (row["id"],),
                    )
                    registrar_auditoria(conn, "CUENTA_BLOQUEADA_INTENTOS_FALLIDOS", usuario_id=row["id"], detalle={"intentos": intentos}, ip=ip, equipo=equipo, resultado="BLOQUEADO")
                    mensaje = "Cuenta bloqueada por demasiados intentos fallidos. Contacte al administrador."
                else:
                    conn.execute("UPDATE usuarios SET intentos_fallidos = ? WHERE id = ?", (intentos, row["id"]))
                    restantes = MAX_INTENTOS_LOGIN - intentos
                    mensaje = f"Usuario o contraseña incorrectos. Le quedan {restantes} intento(s) antes del bloqueo automático de la cuenta."
            registrar_auditoria(conn, "LOGIN_FALLIDO", usuario_id=row["id"] if row else None, detalle={"usuario_ingresado": usuario.strip()}, ip=ip, equipo=equipo, resultado="FALLIDO")
            return templates.TemplateResponse("login.html", {"request": request, "error": mensaje}, status_code=401)
        u = dict(row)
        if u["estado"] != "ACTIVO":
            registrar_auditoria(conn, "LOGIN_FALLIDO", usuario_id=u["id"], detalle={"motivo": f"Estado {u['estado']}"}, ip=ip, equipo=equipo, resultado="DENEGADO")
            return templates.TemplateResponse("login.html", {"request": request, "error": "Su cuenta no está activa. Contacte al administrador."}, status_code=403)
        conn.execute("UPDATE usuarios SET ultimo_login = CURRENT_TIMESTAMP, intentos_fallidos = 0 WHERE id = ?", (u["id"],))
        registrar_auditoria(conn, "LOGIN_EXITOSO", usuario_id=u["id"], ip=ip, equipo=equipo, resultado="OK")
    request.session["user_id"] = u["id"]
    request.session["usuario"] = u["usuario"]
    request.session["rol"] = u["rol"]
    request.session["last_activity"] = datetime.now().timestamp()
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    uid = session_user_id(request)
    if uid:
        with get_db() as conn:
            registrar_auditoria(conn, "LOGOUT", usuario_id=uid, ip=client_ip(request), equipo=ua(request), resultado="OK")
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/registro", response_class=HTMLResponse)
def registro_get(request: Request):
    if session_user_id(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("registro.html", {"request": request, "error": None})


@app.post("/registro", response_class=HTMLResponse)
def registro_post(
    request: Request,
    nombre_apellido: str = Form(...),
    dni_legajo: str = Form(...),
    usuario: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    tipo_registro: str = Form(...),
    _csrf: None = Depends(verificar_csrf),
):
    ip = client_ip(request)
    equipo = ua(request)
    err = validar_username(usuario)
    if err:
        return templates.TemplateResponse("registro.html", {"request": request, "error": err}, status_code=400)
    if password != password2:
        return templates.TemplateResponse("registro.html", {"request": request, "error": "Las contraseñas no coinciden."}, status_code=400)
    if len(password) < 6:
        return templates.TemplateResponse("registro.html", {"request": request, "error": "La contraseña debe tener al menos 6 caracteres."}, status_code=400)
    rol = tipo_registro.upper()
    if rol not in ("JEFE", "BRIGADA"):
        return templates.TemplateResponse("registro.html", {"request": request, "error": "Tipo de registro inválido."}, status_code=400)
    with get_db() as conn:
        if not validar_usuario_unico(conn, usuario):
            return templates.TemplateResponse("registro.html", {"request": request, "error": "El usuario ya existe."}, status_code=400)
        if not puede_registrar_rol(conn, rol):
            return templates.TemplateResponse("registro.html", {"request": request, "error": "No hay cupos disponibles para este tipo de registro. Comuníquese con el administrador."}, status_code=400)
        conn.execute(
            """
            INSERT INTO usuarios (usuario, nombre_apellido, dni_legajo, password_hash, rol, estado)
            VALUES (?, ?, ?, ?, ?, 'PENDIENTE')
            """,
            (usuario.strip(), nombre_apellido.strip(), dni_legajo.strip(), hash_password(password), rol),
        )
        nuevo = conn.execute("SELECT id FROM usuarios WHERE usuario = ?", (usuario.strip(),)).fetchone()
        registrar_auditoria(conn, "REGISTRO_SOLICITADO", usuario_id=nuevo["id"] if nuevo else None, detalle={"usuario_solicitado": usuario.strip(), "nombre_apellido": nombre_apellido.strip(), "dni_legajo": dni_legajo.strip(), "rol_solicitado": rol}, ip=ip, equipo=equipo, resultado="SOLICITUD_REGISTRO")
    return templates.TemplateResponse("registro.html", {"request": request, "error": None, "ok": "Solicitud registrada. Espere aprobación del administrador."})


@app.get("/cambiar_password", response_class=HTMLResponse)
def cambiar_password_get(request: Request, user: dict = Depends(require_login)):
    return templates.TemplateResponse("cambiar_password.html", {"request": request, "user": user, "obligatorio": bool(user.get("debe_cambiar_password"))})


@app.post("/cambiar_password", response_class=HTMLResponse)
async def cambiar_password_post(request: Request, user: dict = Depends(require_login), _csrf: None = Depends(verificar_csrf)):
    form = await request.form()

    actual = (
        form.get("actual")
        or form.get("password_actual")
        or form.get("contrasena_actual")
        or ""
    )

    nueva = (
        form.get("nueva")
        or form.get("password_nueva")
        or form.get("nueva_password")
        or form.get("contrasena_nueva")
        or ""
    )

    nueva2 = (
        form.get("nueva2")
        or form.get("password_nueva2")
        or form.get("repetir_password")
        or form.get("repetir_nueva")
        or form.get("contrasena_nueva2")
        or ""
    )

    obligatorio = bool(user.get("debe_cambiar_password"))

    if not obligatorio:
        if not actual or not verificar_password(str(actual), user["password_hash"]):
            return templates.TemplateResponse(
                "cambiar_password.html",
                {
                    "request": request,
                    "user": user,
                    "obligatorio": False,
                    "error": "Contraseña actual incorrecta.",
                },
                status_code=400,
            )

    nueva = str(nueva)
    nueva2 = str(nueva2)

    if nueva != nueva2:
        return templates.TemplateResponse(
            "cambiar_password.html",
            {
                "request": request,
                "user": user,
                "obligatorio": obligatorio,
                "error": "Las nuevas contraseñas no coinciden.",
            },
            status_code=400,
        )

    if len(nueva) < 6:
        return templates.TemplateResponse(
            "cambiar_password.html",
            {
                "request": request,
                "user": user,
                "obligatorio": obligatorio,
                "error": "La nueva contraseña debe tener al menos 6 caracteres.",
            },
            status_code=400,
        )

    with get_db() as conn:
        conn.execute(
            "UPDATE usuarios SET password_hash = ?, debe_cambiar_password = 0 WHERE id = ?",
            (hash_password(nueva), user["id"]),
        )
        registrar_auditoria(
            conn,
            "PASSWORD_CAMBIADA",
            usuario_id=user["id"],
            ip=client_ip(request),
            equipo=ua(request),
            resultado="OK",
        )

    request.session["sigemep_flash"] = "Contraseña actualizada correctamente."
    return RedirectResponse("/", status_code=302)



def _stats_dashboard_admin(conn) -> dict[str, Any]:
    n_mem = conn.execute("SELECT COUNT(*) FROM memorandos WHERE activo = 1").fetchone()[0]
    n_act = conn.execute("SELECT COUNT(*) FROM usuarios WHERE estado = 'ACTIVO'").fetchone()[0]
    n_pend = conn.execute("SELECT COUNT(*) FROM usuarios WHERE estado = 'PENDIENTE'").fetchone()[0]
    n_bus = conn.execute("SELECT COUNT(*) FROM busquedas").fetchone()[0]
    n_desc = conn.execute("SELECT COUNT(*) FROM auditoria WHERE accion = 'PDF_DESCARGADO'").fetchone()[0]
    n_den = conn.execute("SELECT COUNT(*) FROM auditoria WHERE accion IN ('DESCARGA_DENEGADA','ACCESO_DENEGADO')").fetchone()[0]
    ult_mov = conn.execute("""
        SELECT a.*, u.usuario AS u_nom FROM auditoria a
        LEFT JOIN usuarios u ON u.id = a.usuario_id
        ORDER BY a.fecha_hora DESC LIMIT 12
    """).fetchall()
    ult_bus = conn.execute("""
        SELECT b.*, u.usuario AS u_nom FROM busquedas b
        JOIN usuarios u ON u.id = b.usuario_id
        ORDER BY b.fecha_hora DESC LIMIT 10
    """).fetchall()
    ult_desc = conn.execute("""
        SELECT a.*, u.usuario AS u_nom, m.nombre_archivo
        FROM auditoria a
        JOIN usuarios u ON u.id = a.usuario_id
        LEFT JOIN memorandos m ON m.id = a.memorando_id
        WHERE a.accion = 'PDF_DESCARGADO'
        ORDER BY a.fecha_hora DESC LIMIT 10
    """).fetchall()
    pendientes_lista = conn.execute("""
        SELECT id, usuario, nombre_apellido, dni_legajo, rol, estado, creado_en
        FROM usuarios
        WHERE estado = 'PENDIENTE'
        ORDER BY creado_en DESC
        LIMIT 10
    """).fetchall()

    ult_mov_dict = [dict(r) for r in ult_mov]
    ult_bus_dict = [dict(r) for r in ult_bus]
    ult_desc_dict = [dict(r) for r in ult_desc]
    pendientes_dict = [dict(r) for r in pendientes_lista]

    return {
        "n_memorandos": n_mem,
        "n_activos": n_act,
        "n_pendientes": n_pend,
        "n_busquedas": n_bus,
        "n_descargas": n_desc,
        "n_denegados": n_den,
        "ult_mov": ult_mov_dict,
        "ult_bus": ult_bus_dict,
        "ult_desc": ult_desc_dict,
        "pdf_dir": get_config(conn, "pdf_dir", ""),
        "total_memorandos": n_mem,
        "usuarios_activos": n_act,
        "usuarios_pendientes": n_pend,
        "intentos_denegados": n_den,
        "ultimos_movimientos": ult_mov_dict,
        "usuarios_pendientes_lista": pendientes_dict,
    }


def _ultimos_memorandos_usuario(conn, uid: int, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT m.id, m.nombre_archivo, MAX(a.fecha_hora) AS visto
        FROM auditoria a
        JOIN memorandos m ON m.id = a.memorando_id
        WHERE a.usuario_id = ? AND a.accion = 'PRIMERA_HOJA_VISUALIZADA'
        GROUP BY m.id, m.nombre_archivo
        ORDER BY visto DESC LIMIT ?
    """, (uid, limit)).fetchall()
    return [dict(r) for r in rows]


@app.get("/dashboard/admin", response_class=HTMLResponse)
def dashboard_admin(request: Request, user: dict = Depends(require_admin)):
    flash = request.session.pop("sigemep_flash", None)
    with get_db() as conn:
        data = _stats_dashboard_admin(conn)
    return templates.TemplateResponse("dashboard_admin.html", {"request": request, "user": user, "flash": flash, **data})


@app.get("/dashboard/jefe", response_class=HTMLResponse)
def dashboard_jefe(request: Request, user: dict = Depends(require_roles("JEFE"))):
    with get_db() as conn:
        ult = _ultimos_memorandos_usuario(conn, user["id"])
    return templates.TemplateResponse("dashboard_jefe.html", {"request": request, "user": user, "ultimos": ult})


@app.get("/dashboard/brigada", response_class=HTMLResponse)
def dashboard_brigada(request: Request, user: dict = Depends(require_roles("BRIGADA")),
                      memo_creado: Optional[str] = Query(None)):
    with get_db() as conn:
        ult = _ultimos_memorandos_usuario(conn, user["id"])
    return templates.TemplateResponse("dashboard_brigada.html", {
        "request": request, "user": user, "ultimos": ult, "memo_creado": memo_creado,
    })


def _page_window(page: int, total_pages: int):
    start = max(1, page - 3)
    end = min(total_pages, page + 3)
    return list(range(start, end + 1))

@app.get("/criterios_busqueda", response_class=HTMLResponse)
def criterios_busqueda(request: Request, user: dict = Depends(require_roles("ADMIN", "JEFE", "BRIGADA"))):
    return templates.TemplateResponse("criterios_busqueda.html", {"request": request, "user": user})

@app.get("/buscar", response_class=HTMLResponse)
def buscar_get(request: Request, user: dict = Depends(require_roles("ADMIN", "JEFE", "BRIGADA"))):
    return templates.TemplateResponse("buscar.html", {"request": request, "user": user})

def _sigemep_page_window(page: int, total_pages: int):
    page = int(page or 1)
    total_pages = int(total_pages or 1)
    if total_pages <= 9:
        return list(range(1, total_pages + 1))
    pages = {1, 2, total_pages - 1, total_pages, page - 2, page - 1, page, page + 1, page + 2}
    pages = sorted(p for p in pages if 1 <= p <= total_pages)
    out = []
    last = None
    for p in pages:
        if last is not None and p - last > 1:
            out.append("...")
        out.append(p)
        last = p
    return out


@app.post("/buscar")
def buscar_post(
    request: Request,
    q: str = Form(""),
    campo: str = Form("todo"),
    fecha_desde: str = Form(""),
    fecha_hasta: str = Form(""),
    paginas_min: int = Form(0),
    paginas_max: int = Form(0),
    user: dict = Depends(require_roles("ADMIN", "JEFE", "BRIGADA")),
    _csrf: None = Depends(verificar_csrf),
):
    require_password_ok(request, user)
    q = (q or "").strip()
    hay_filtros = bool((campo and campo != "todo") or fecha_desde or fecha_hasta or paginas_min or paginas_max)
    if not q and not hay_filtros:
        return RedirectResponse("/buscar", status_code=302)
    params: dict[str, str] = {"q": q, "page": "1"}
    if campo and campo != "todo":
        params["campo"] = campo
    if fecha_desde:
        params["fecha_desde"] = fecha_desde
    if fecha_hasta:
        params["fecha_hasta"] = fecha_hasta
    if paginas_min:
        params["paginas_min"] = str(paginas_min)
    if paginas_max:
        params["paginas_max"] = str(paginas_max)
    return RedirectResponse(f"/buscar/resultados?{urlencode(params)}", status_code=302)


@app.get("/buscar/resultados", response_class=HTMLResponse)
def buscar_resultados_get(
    request: Request,
    user: dict = Depends(require_roles("ADMIN", "JEFE", "BRIGADA")),
    q: str = Query(""),
    page: int = Query(1),
    campo: str = Query("todo"),
    fecha_desde: str = Query(""),
    fecha_hasta: str = Query(""),
    paginas_min: int = Query(0),
    paginas_max: int = Query(0),
):
    require_password_ok(request, user)
    q = (q or "").strip()
    hay_filtros = bool((campo and campo != "todo") or fecha_desde or fecha_hasta or paginas_min or paginas_max)
    if not q and not hay_filtros:
        return RedirectResponse("/buscar", status_code=302)

    page_size = 20
    page = max(1, int(page or 1))

    todos = list(buscar_memorandos(
        q,
        limit=5000,
        campo=campo or "todo",
        fecha_desde=fecha_desde or "",
        fecha_hasta=fecha_hasta or "",
        paginas_min=paginas_min or 0,
        paginas_max=paginas_max or 0,
    ))

    total = len(todos)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    resultados = todos[start:start + page_size]

    if page == 1:
        try:
            with get_db() as conn:
                registrar_busqueda(conn, user["id"], q, total, client_ip(request))
                registrar_auditoria(conn, "BUSQUEDA_REALIZADA", usuario_id=user["id"], detalle={"q": q, "cantidad_resultados": total, "paginacion": True}, ip=client_ip(request), equipo=ua(request), resultado="OK")
        except Exception:
            pass

    filtros_params: dict[str, str] = {}
    if campo and campo != "todo":
        filtros_params["campo"] = campo
    if fecha_desde:
        filtros_params["fecha_desde"] = fecha_desde
    if fecha_hasta:
        filtros_params["fecha_hasta"] = fecha_hasta
    if paginas_min:
        filtros_params["paginas_min"] = str(paginas_min)
    if paginas_max:
        filtros_params["paginas_max"] = str(paginas_max)
    filtros_url = ("&" + urlencode(filtros_params)) if filtros_params else ""

    return templates.TemplateResponse("resultados.html", {
        "request": request, "user": user, "q": q,
        "resultados": resultados, "total": total, "page": page,
        "page_size": page_size, "total_pages": total_pages,
        "page_numbers": _sigemep_page_window(page, total_pages),
        "puede_descargar": user["rol"] in ROLES_DESCARGA_PDF,
        "campo": campo or "todo",
        "fecha_desde": fecha_desde or "",
        "fecha_hasta": fecha_hasta or "",
        "paginas_min": paginas_min or 0,
        "paginas_max": paginas_max or 0,
        "filtros_url": filtros_url,
        "filtros_activos": bool(filtros_params),
    })

@app.get("/preview/{mem_id}")
def preview_memorando(mem_id: int, request: Request, user: dict = Depends(require_roles("ADMIN", "JEFE", "BRIGADA"))):
    with get_db() as conn:
        m = conn.execute("SELECT * FROM memorandos WHERE id = ? AND activo = 1", (mem_id,)).fetchone()
    if not m or not m["primera_hoja_img"]:
        raise HTTPException(404)
    p = Path(m["primera_hoja_img"])
    if not p.is_file():
        raise HTTPException(404)
    return Response(p.read_bytes(), media_type="image/png")


@app.get("/ver_primera_hoja/{mem_id}", response_class=HTMLResponse)
def ver_primera_hoja(mem_id: int, request: Request, user: dict = Depends(require_roles("ADMIN", "JEFE", "BRIGADA"))):
    with get_db() as conn:
        m = conn.execute("SELECT * FROM memorandos WHERE id = ? AND activo = 1", (mem_id,)).fetchone()
        if not m:
            raise HTTPException(404)
        ruta = ruta_absoluta_segura(m["ruta_archivo"], conn)
        if not ruta:
            raise HTTPException(404)
        preview = Path(m["primera_hoja_img"]) if m["primera_hoja_img"] else None
        img_bytes = imagen_primera_hoja_con_marca(ruta, preview, user["usuario"], user["rol"], client_ip(request))
        registrar_auditoria(conn, "PRIMERA_HOJA_VISUALIZADA", usuario_id=user["id"], memorando_id=mem_id, detalle={"archivo": m["nombre_archivo"]}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return templates.TemplateResponse("visor_primera_hoja.html", {"request": request, "user": user, "mem": dict(m), "imagen_b64": base64.b64encode(img_bytes).decode("ascii")})


@app.get("/descargar/{mem_id}")
def descargar(mem_id: int, request: Request, user: dict = Depends(require_login)):
    require_password_ok(request, user)
    with get_db() as conn:
        m = conn.execute("SELECT * FROM memorandos WHERE id = ? AND activo = 1", (mem_id,)).fetchone()
        if not m:
            raise HTTPException(404)
        if user["rol"] not in ROLES_DESCARGA_PDF:
            registrar_auditoria(conn, "DESCARGA_DENEGADA", usuario_id=user["id"], memorando_id=mem_id, detalle={"archivo": m["nombre_archivo"]}, ip=client_ip(request), equipo=ua(request), resultado="DENEGADO")
            return RedirectResponse("/acceso_denegado", status_code=302)
        ruta = ruta_absoluta_segura(m["ruta_archivo"], conn)
        if not ruta:
            raise HTTPException(404)
        registrar_auditoria(conn, "PDF_DESCARGADO", usuario_id=user["id"], memorando_id=mem_id, detalle={"archivo": m["nombre_archivo"]}, ip=client_ip(request), equipo=ua(request), resultado="OK")

    def iterfile():
        with open(ruta, "rb") as f:
            yield from iter(lambda: f.read(1024 * 1024), b"")

    return StreamingResponse(iterfile(), media_type="application/pdf", headers={"Content-Disposition": _safe_content_disposition_inline(m["nombre_archivo"])})

def _safe_content_disposition_inline(nombre_archivo: str) -> str:
    import re
    from urllib.parse import quote

    nombre = str(nombre_archivo or "memorando.pdf").strip()
    if not nombre.lower().endswith(".pdf"):
        nombre += ".pdf"

    ascii_name = nombre.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_name).strip()
    if not ascii_name:
        ascii_name = "memorando.pdf"

    ascii_name = ascii_name.replace('"', "_").replace("\\", "_").replace("/", "_")
    utf8_name = quote(nombre)
    return "inline; filename=\"" + ascii_name + "\"; filename*=UTF-8''" + utf8_name

@app.get("/ver_pdf_completo/{mem_id}", response_class=HTMLResponse)
def ver_pdf_completo(mem_id: int, request: Request, user: dict = Depends(require_login)):
    require_password_ok(request, user)

    with get_db() as conn:
        m = conn.execute("SELECT * FROM memorandos WHERE id = ? AND activo = 1", (mem_id,)).fetchone()

        if not m:
            raise HTTPException(404)

        if user["rol"] not in ROLES_DESCARGA_PDF:
            registrar_auditoria(
                conn,
                "PDF_COMPLETO_DENEGADO",
                usuario_id=user["id"],
                memorando_id=mem_id,
                detalle={"archivo": m["nombre_archivo"]},
                ip=client_ip(request),
                equipo=ua(request),
                resultado="DENEGADO",
            )
            return RedirectResponse("/acceso_denegado", status_code=302)

        ruta = ruta_absoluta_segura(m["ruta_archivo"], conn)
        if not ruta:
            registrar_auditoria(
                conn,
                "PDF_COMPLETO_NO_ENCONTRADO",
                usuario_id=user["id"],
                memorando_id=mem_id,
                detalle={"archivo": m["nombre_archivo"], "ruta_archivo": m["ruta_archivo"]},
                ip=client_ip(request),
                equipo=ua(request),
                resultado="ERROR",
            )
            raise HTTPException(404)

        registrar_auditoria(
            conn,
            "PDF_COMPLETO_VISUALIZADO",
            usuario_id=user["id"],
            memorando_id=mem_id,
            detalle={"archivo": m["nombre_archivo"]},
            ip=client_ip(request),
            equipo=ua(request),
            resultado="OK",
        )

        memorando = dict(m)

    return templates.TemplateResponse(
        "ver_pdf_completo.html",
        {
            "request": request,
            "user": user,
            "memorando": memorando,
        },
    )


@app.get("/pdf_completo_stream/{mem_id}")
def pdf_completo_stream(mem_id: int, request: Request, user: dict = Depends(require_login)):
    require_password_ok(request, user)

    with get_db() as conn:
        m = conn.execute("SELECT * FROM memorandos WHERE id = ? AND activo = 1", (mem_id,)).fetchone()

        if not m:
            raise HTTPException(404)

        if user["rol"] not in ROLES_DESCARGA_PDF:
            registrar_auditoria(
                conn,
                "PDF_COMPLETO_STREAM_DENEGADO",
                usuario_id=user["id"],
                memorando_id=mem_id,
                detalle={"archivo": m["nombre_archivo"]},
                ip=client_ip(request),
                equipo=ua(request),
                resultado="DENEGADO",
            )
            return RedirectResponse("/acceso_denegado", status_code=302)

        ruta = ruta_absoluta_segura(m["ruta_archivo"], conn)
        if not ruta:
            raise HTTPException(404)

        nombre_archivo = m["nombre_archivo"]

    def iterfile():
        with open(ruta, "rb") as f:
            yield from iter(lambda: f.read(1024 * 1024), b"")

    return StreamingResponse(
        iterfile(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": _safe_content_disposition_inline(nombre_archivo),
            "X-Content-Type-Options": "nosniff",
        },
    )

def asegurar_columna_eliminado_usuarios(conn):
    """
    Asegura la columna usuarios.eliminado para borrado lógico.
    Es segura para ejecutar varias veces.
    """
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(usuarios)").fetchall()]

        if "eliminado" not in cols:
            conn.execute("ALTER TABLE usuarios ADD COLUMN eliminado INTEGER NOT NULL DEFAULT 0")

        conn.execute(
            "UPDATE usuarios SET eliminado = 1 WHERE UPPER(COALESCE(estado, '')) = 'ELIMINADO'"
        )
    except Exception:
        pass

@app.get("/admin/usuarios", response_class=HTMLResponse)
def admin_usuarios(request: Request, user: dict = Depends(require_roles("ADMIN", "JEFE")), q: str = "", rol: str = "", estado: str = ""):
    require_password_ok(request, user)
    flash = request.session.pop("sigemep_flash", None)
    err = request.query_params.get("error")
    es_admin = user["rol"] == "ADMIN"
    sql = "SELECT * FROM usuarios WHERE 1=1"
    if not estado:
        sql += " AND COALESCE(eliminado, 0) = 0"
    params: list[Any] = []
    if q:
        sql += " AND (usuario LIKE ? OR nombre_apellido LIKE ? OR dni_legajo LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if rol:
        sql += " AND rol = ?"
        params.append(rol)
    if estado:
        sql += " AND estado = ?"
        params.append(estado)
    sql += " ORDER BY id DESC"
    with get_db() as conn:
        asegurar_columna_eliminado_usuarios(conn)
        rows = conn.execute(sql, params).fetchall()
    return templates.TemplateResponse("usuarios.html", {"request": request, "user": user, "usuarios": [dict(r) for r in rows], "flash": flash, "query_error": err, "q": q, "rol": rol, "estado": estado, "es_admin": es_admin})


@app.get("/admin/usuario/{uid}/movimientos", response_class=HTMLResponse)
def admin_usuario_movimientos(uid: int, request: Request, user: dict = Depends(require_admin)):
    with get_db() as conn:
        objetivo = conn.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
        if not objetivo:
            raise HTTPException(404)
    rows = listar_auditoria(usuario_id=uid, limit=1000)
    return templates.TemplateResponse("usuario_movimientos.html", {"request": request, "user": user, "objetivo": dict(objetivo), "rows": rows})


@app.get("/admin/solicitudes", response_class=HTMLResponse)
def admin_solicitudes(request: Request, user: dict = Depends(require_admin)):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM usuarios WHERE estado = 'PENDIENTE' ORDER BY creado_en DESC").fetchall()
    return templates.TemplateResponse("solicitudes.html", {"request": request, "user": user, "pendientes": [dict(r) for r in rows]})


def _usuario_row(conn, uid: int):
    return conn.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()


@app.post("/admin/usuario/{uid}/aprobar")
def admin_aprobar(request: Request, uid: int, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row and row["estado"] == "PENDIENTE":
            conn.execute("UPDATE usuarios SET estado = 'ACTIVO', aprobado_por = ?, aprobado_en = CURRENT_TIMESTAMP WHERE id = ?", (user["id"], uid))
            registrar_auditoria(conn, "USUARIO_APROBADO", usuario_id=user["id"], detalle={"aprobado_id": uid, "usuario": row["usuario"]}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/solicitudes", status_code=302)


@app.post("/admin/usuario/{uid}/rechazar")
def admin_rechazar(request: Request, uid: int, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row:
            conn.execute("UPDATE usuarios SET estado = 'RECHAZADO' WHERE id = ?", (uid,))
            registrar_auditoria(conn, "USUARIO_RECHAZADO", usuario_id=user["id"], detalle={"rechazado_id": uid, "usuario": row["usuario"]}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/solicitudes", status_code=302)


@app.post("/admin/usuario/{uid}/bloquear")
def admin_bloquear(request: Request, uid: int, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row and row["usuario"] != "admin":
            conn.execute("UPDATE usuarios SET estado = 'BLOQUEADO' WHERE id = ?", (uid,))
            registrar_auditoria(conn, "USUARIO_BLOQUEADO", usuario_id=user["id"], detalle={"bloqueado_id": uid}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/usuarios", status_code=302)


@app.post("/admin/usuario/{uid}/activar")
def admin_activar(request: Request, uid: int, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row:
            if row["estado"] == "BLOQUEADO" and not puede_registrar_rol(conn, row["rol"]):
                return RedirectResponse("/admin/usuarios?error=cupo", status_code=302)
            conn.execute("UPDATE usuarios SET estado = 'ACTIVO' WHERE id = ?", (uid,))
            registrar_auditoria(conn, "USUARIO_ACTIVADO", usuario_id=user["id"], detalle={"activado_id": uid}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/usuarios", status_code=302)


@app.post("/admin/usuario/{uid}/rol")
async def admin_cambiar_rol(request: Request, uid: int, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    form = await request.form()
    nuevo_rol = str(form.get("nuevo_rol") or form.get("rol") or "").upper().strip()

    if nuevo_rol not in ("ADMIN", "JEFE", "BRIGADA"):
        request.session["sigemep_flash"] = "Rol inválido. No se realizaron cambios."
        return RedirectResponse("/admin/usuarios", status_code=302)

    with get_db() as conn:
        row = _usuario_row(conn, uid)

        if not row:
            request.session["sigemep_flash"] = "Usuario no encontrado."
            return RedirectResponse("/admin/usuarios", status_code=302)

        if row["usuario"] == "admin":
            request.session["sigemep_flash"] = "No se puede cambiar el rol del usuario admin principal."
            return RedirectResponse("/admin/usuarios", status_code=302)

        if row["estado"] == "ELIMINADO":
            request.session["sigemep_flash"] = "No se puede cambiar el rol de un usuario eliminado."
            return RedirectResponse("/admin/usuarios", status_code=302)

        if nuevo_rol != row["rol"]:
            conn.execute(
                "UPDATE usuarios SET rol = ? WHERE id = ?",
                (nuevo_rol, uid),
            )
            registrar_auditoria(
                conn,
                "ROL_MODIFICADO",
                usuario_id=user["id"],
                detalle={
                    "uid": uid,
                    "usuario": row["usuario"],
                    "rol_anterior": row["rol"],
                    "nuevo_rol": nuevo_rol,
                },
                ip=client_ip(request),
                equipo=ua(request),
                resultado="OK",
            )
            request.session["sigemep_flash"] = f"Rol de «{row['usuario']}» actualizado a {nuevo_rol}."
        else:
            request.session["sigemep_flash"] = "El usuario ya tenía ese rol."

    return RedirectResponse("/admin/usuarios", status_code=302)


@app.post("/admin/usuario/{uid}/permiso_reservados")
def admin_usuario_permiso_reservados(uid: int, request: Request, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    with get_db() as conn:
        row = conn.execute("SELECT usuario, permiso_reservados FROM usuarios WHERE id = ?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404)
        nuevo_valor = 0 if row["permiso_reservados"] else 1
        conn.execute("UPDATE usuarios SET permiso_reservados = ? WHERE id = ?", (nuevo_valor, uid))
        registrar_auditoria(
            conn,
            "PERMISO_RESERVADOS_OTORGADO" if nuevo_valor else "PERMISO_RESERVADOS_REVOCADO",
            usuario_id=user["id"],
            detalle={"usuario_afectado": row["usuario"], "uid": uid},
            ip=client_ip(request),
            equipo=ua(request),
            resultado="OK",
        )
    request.session["sigemep_flash"] = "Permiso de Reservados actualizado."
    return RedirectResponse("/admin/usuarios", status_code=302)


@app.post("/admin/usuario/{uid}/password_temp")
def admin_password_temp(request: Request, uid: int, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if not row or row["usuario"] == "admin":
            return RedirectResponse("/admin/usuarios", status_code=302)
        plain = generar_temp_password()
        conn.execute("UPDATE usuarios SET password_hash = ?, debe_cambiar_password = 1 WHERE id = ?", (hash_password(plain), uid))
        registrar_auditoria(conn, "PASSWORD_TEMPORAL_GENERADA", usuario_id=user["id"], detalle={"objetivo_id": uid, "usuario_objetivo": row["usuario"]}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    request.session["sigemep_flash"] = f"Contraseña temporal para «{row['usuario']}»: {plain} (guárdela ahora; no se volverá a mostrar)."
    return RedirectResponse("/admin/usuarios", status_code=302)



@app.post("/admin/usuario/{uid}/eliminar")
def admin_eliminar_usuario(request: Request, uid: int, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    with get_db() as conn:
        asegurar_columna_eliminado_usuarios(conn)

        row = conn.execute(
            "SELECT id, usuario, rol, estado, COALESCE(eliminado, 0) AS eliminado FROM usuarios WHERE id = ?",
            (uid,)
        ).fetchone()

        if not row:
            request.session["sigemep_flash"] = "Usuario no encontrado."
            return RedirectResponse("/admin/usuarios", status_code=302)

        usuario_objetivo = row["usuario"]
        rol_objetivo = row["rol"]
        estado_objetivo = row["estado"]

        if usuario_objetivo == "admin":
            request.session["sigemep_flash"] = "No se puede eliminar el usuario admin principal."
            return RedirectResponse("/admin/usuarios", status_code=302)

        if int(row["id"]) == int(user["id"]):
            request.session["sigemep_flash"] = "No se puede eliminar el usuario actualmente logueado."
            return RedirectResponse("/admin/usuarios", status_code=302)

        if int(row["eliminado"] or 0) == 1:
            request.session["sigemep_flash"] = f"El usuario «{usuario_objetivo}» ya estaba eliminado."
            return RedirectResponse("/admin/usuarios", status_code=302)

        conn.execute(
            "UPDATE usuarios SET eliminado = 1 WHERE id = ?",
            (uid,)
        )

        try:
            registrar_auditoria(
                conn,
                "USUARIO_ELIMINADO",
                usuario_id=user["id"],
                detalle={
                    "usuario_eliminado_id": uid,
                    "usuario_eliminado": usuario_objetivo,
                    "rol": rol_objetivo,
                    "estado": estado_objetivo,
                    "eliminado": 1,
                },
                ip=client_ip(request),
                equipo=ua(request),
                resultado="OK",
            )
        except TypeError:
            try:
                registrar_auditoria(
                    conn,
                    "USUARIO_ELIMINADO",
                    usuario_id=user["id"],
                    detalle={
                        "usuario_eliminado_id": uid,
                        "usuario_eliminado": usuario_objetivo,
                        "rol": rol_objetivo,
                        "estado": estado_objetivo,
                        "eliminado": 1,
                    },
                    ip=client_ip(request),
                    resultado="OK",
                )
            except Exception:
                pass
        except Exception:
            pass

    request.session["sigemep_flash"] = f"Usuario «{usuario_objetivo}» eliminado correctamente."
    return RedirectResponse("/admin/usuarios", status_code=302)

@app.get("/jefe/brigada_movimientos", response_class=HTMLResponse)
def jefe_brigada_movimientos(
    request: Request,
    user: dict = Depends(require_roles("JEFE")),
    page: int = Query(1),
):
    page_size = 25
    page = max(1, int(page or 1))
    offset = (page - 1) * page_size

    with get_db() as conn:
        try:
            asegurar_columna_eliminado_usuarios(conn)
            filtro_eliminado = " AND COALESCE(u.eliminado, 0) = 0 "
            filtro_eliminado_simple = " AND COALESCE(eliminado, 0) = 0 "
        except Exception:
            filtro_eliminado = ""
            filtro_eliminado_simple = ""

        brigadas = conn.execute(f"""
            SELECT id, usuario, nombre_apellido, dni_legajo, estado, ultimo_login
            FROM usuarios
            WHERE rol = 'BRIGADA'
              {filtro_eliminado_simple}
            ORDER BY usuario
        """).fetchall()

        total = conn.execute(f"""
            SELECT COUNT(*)
            FROM auditoria a
            JOIN usuarios u ON u.id = a.usuario_id
            WHERE u.rol = 'BRIGADA'
              {filtro_eliminado}
        """).fetchone()[0]

        rows = conn.execute(f"""
            SELECT a.id, a.fecha_hora, a.usuario_id, u.usuario AS usuario_nombre,
                   u.nombre_apellido, a.accion, a.memorando_id, a.ip,
                   a.resultado, a.detalle
            FROM auditoria a
            JOIN usuarios u ON u.id = a.usuario_id
            WHERE u.rol = 'BRIGADA'
              {filtro_eliminado}
            ORDER BY a.fecha_hora DESC
            LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    return templates.TemplateResponse(
        "jefe_brigada_movimientos.html",
        {
            "request": request,
            "user": user,
            "brigadas": [dict(r) for r in brigadas],
            "rows": [dict(r) for r in rows],
            "objetivo": None,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "page_numbers": _page_window(page, total_pages),
            "base_url": "/jefe/brigada_movimientos",
        },
    )

@app.get("/jefe/brigada/{uid}/movimientos", response_class=HTMLResponse)
def jefe_brigada_usuario_movimientos(
    uid: int,
    request: Request,
    user: dict = Depends(require_roles("JEFE")),
    page: int = Query(1),
):
    page_size = 25
    page = max(1, int(page or 1))
    offset = (page - 1) * page_size

    with get_db() as conn:
        try:
            asegurar_columna_eliminado_usuarios(conn)
            filtro_eliminado = " AND COALESCE(eliminado, 0) = 0 "
        except Exception:
            filtro_eliminado = ""

        objetivo = conn.execute(f"""
            SELECT id, usuario, nombre_apellido, dni_legajo, estado, ultimo_login
            FROM usuarios
            WHERE id = ?
              AND rol = 'BRIGADA'
              {filtro_eliminado}
        """, (uid,)).fetchone()

        if not objetivo:
            registrar_auditoria(
                conn,
                "ACCESO_DENEGADO",
                usuario_id=user["id"],
                detalle={"motivo": "JEFE intentó ver movimientos de usuario no BRIGADA", "uid": uid},
                ip=client_ip(request),
                equipo=ua(request),
                resultado="DENEGADO",
            )
            return RedirectResponse("/acceso_denegado", status_code=302)

        brigadas = conn.execute(f"""
            SELECT id, usuario, nombre_apellido, dni_legajo, estado, ultimo_login
            FROM usuarios
            WHERE rol = 'BRIGADA'
              {filtro_eliminado}
            ORDER BY usuario
        """).fetchall()

        total = conn.execute("SELECT COUNT(*) FROM auditoria WHERE usuario_id = ?", (uid,)).fetchone()[0]

        rows = conn.execute("""
            SELECT a.id, a.fecha_hora, a.usuario_id, u.usuario AS usuario_nombre,
                   u.nombre_apellido, a.accion, a.memorando_id, a.ip,
                   a.resultado, a.detalle
            FROM auditoria a
            JOIN usuarios u ON u.id = a.usuario_id
            WHERE a.usuario_id = ?
            ORDER BY a.fecha_hora DESC
            LIMIT ? OFFSET ?
        """, (uid, page_size, offset)).fetchall()

        if page == 1:
            registrar_auditoria(
                conn,
                "JEFE_CONSULTO_MOVIMIENTOS_BRIGADA",
                usuario_id=user["id"],
                detalle={"brigada_id": uid, "brigada_usuario": objetivo["usuario"]},
                ip=client_ip(request),
                equipo=ua(request),
                resultado="OK",
            )

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    return templates.TemplateResponse(
        "jefe_brigada_movimientos.html",
        {
            "request": request,
            "user": user,
            "brigadas": [dict(r) for r in brigadas],
            "rows": [dict(r) for r in rows],
            "objetivo": dict(objetivo),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "page_numbers": _page_window(page, total_pages),
            "base_url": f"/jefe/brigada/{uid}/movimientos",
        },
    )

@app.get("/admin/memorandos", response_class=HTMLResponse)
def admin_memorandos(request: Request, user: dict = Depends(require_admin)):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM memorandos ORDER BY nombre_archivo").fetchall()
    return templates.TemplateResponse("memorandos_admin.html", {"request": request, "user": user, "memorandos": [dict(r) for r in rows]})


@app.get("/admin/reindexar", response_class=HTMLResponse)
def admin_reindexar_get(request: Request, user: dict = Depends(require_admin)):
    with get_db() as conn:
        pdf_dir = get_config(conn, "pdf_dir", str(carpeta_pdf_actual(conn)))
        n_mem = conn.execute("SELECT COUNT(*) FROM memorandos WHERE activo = 1").fetchone()[0]
        last = conn.execute("SELECT * FROM auditoria WHERE accion IN ('INDEXACION_RAPIDA_FINALIZADA','REINDEXACION_COMPLETA_FINALIZADA') ORDER BY fecha_hora DESC LIMIT 1").fetchone()
    return templates.TemplateResponse("reindexar.html", {"request": request, "user": user, "pdf_dir": pdf_dir, "n_memorandos": n_mem, "ultima": dict(last) if last else None})


@app.post("/admin/config/carpeta_pdf")
def admin_config_carpeta_pdf(request: Request, user: dict = Depends(require_admin), pdf_dir: str = Form(...), _csrf: None = Depends(verificar_csrf)):
    ruta = Path(pdf_dir.strip())
    if not ruta.exists() or not ruta.is_dir():
        request.session["sigemep_flash"] = f"La carpeta no existe o no es válida: {ruta}"
        return RedirectResponse("/admin/reindexar", status_code=302)
    with get_db() as conn:
        anterior = get_config(conn, "pdf_dir", "")
        set_config(conn, "pdf_dir", str(ruta))
        registrar_auditoria(conn, "CARPETA_PDF_ACTUALIZADA", usuario_id=user["id"], detalle={"anterior": anterior, "nueva": str(ruta)}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    request.session["sigemep_flash"] = "Carpeta PDF actualizada correctamente."
    return RedirectResponse("/admin/reindexar", status_code=302)


@app.post("/admin/reindexar")
def admin_reindexar_legacy(request: Request, user: dict = Depends(require_admin), _csrf: None = Depends(verificar_csrf)):
    return RedirectResponse("/admin/reindexar", status_code=302)


@app.post("/admin/reindexar/iniciar")
def admin_reindexar_iniciar(request: Request, user: dict = Depends(require_admin), modo: str = Query("rapida"), _csrf: None = Depends(verificar_csrf)):
    force = modo == "completa"
    with INDEX_JOBS_LOCK:
        for existing_id, job in INDEX_JOBS.items():
            if job.get("estado") in {"preparando", "ejecutando"}:
                return JSONResponse({"job_id": existing_id, **dict(job)})
    job_id = uuid.uuid4().hex[:12]
    _set_index_job(job_id, {"estado": "preparando", "modo": "completa" if force else "rapida", "total": 0, "procesados": 0, "sin_cambios": 0, "nuevos": 0, "actualizados": 0, "no_encontrados": 0, "errores": 0, "archivo_actual": "", "mensaje": "Iniciando tarea..."})
    t = threading.Thread(target=_index_worker, args=(job_id, user["id"], client_ip(request), ua(request), force), daemon=True)
    t.start()
    return JSONResponse({"job_id": job_id, **_get_index_job(job_id)})


@app.get("/admin/reindexar/estado/{job_id}")
def admin_reindexar_estado(job_id: str, request: Request, user: dict = Depends(require_admin)):
    job = _get_index_job(job_id)
    if not job:
        return JSONResponse({"estado": "no_encontrado", "mensaje": "Tarea no encontrada."}, status_code=404)
    return JSONResponse({"job_id": job_id, **job})


# ── RESERVADOS — INDEXACIÓN (ADMIN) ───────────────────────────────

@app.get("/admin/reservados/reindexar", response_class=HTMLResponse)
def admin_reservados_reindexar_get(request: Request, user: dict = Depends(require_admin)):
    with get_db() as conn:
        reservados_dir = get_config(conn, "reservados_dir", str(carpeta_reservados_actual(conn)))
        n_reservados = conn.execute("SELECT COUNT(*) FROM reservados WHERE activo = 1").fetchone()[0]
        last = conn.execute("SELECT * FROM auditoria WHERE accion IN ('INDEXACION_RESERVADOS_RAPIDA_FINALIZADA','REINDEXACION_RESERVADOS_COMPLETA_FINALIZADA') ORDER BY fecha_hora DESC LIMIT 1").fetchone()
    return templates.TemplateResponse("reservados_reindexar.html", {"request": request, "user": user, "reservados_dir": reservados_dir, "n_reservados": n_reservados, "ultima": dict(last) if last else None})


@app.post("/admin/reservados/reindexar/iniciar")
def admin_reservados_reindexar_iniciar(request: Request, user: dict = Depends(require_admin), modo: str = Query("rapida"), _csrf: None = Depends(verificar_csrf)):
    force = modo == "completa"
    with RESERVADOS_INDEX_JOBS_LOCK:
        for existing_id, job in RESERVADOS_INDEX_JOBS.items():
            if job.get("estado") in {"preparando", "ejecutando"}:
                return JSONResponse({"job_id": existing_id, **dict(job)})
    job_id = uuid.uuid4().hex[:12]
    _set_reservados_index_job(job_id, {"estado": "preparando", "modo": "completa" if force else "rapida", "total": 0, "procesados": 0, "sin_cambios": 0, "nuevos": 0, "actualizados": 0, "no_encontrados": 0, "errores": 0, "archivo_actual": "", "mensaje": "Iniciando tarea..."})
    t = threading.Thread(target=_index_reservados_worker, args=(job_id, user["id"], client_ip(request), ua(request), force), daemon=True)
    t.start()
    return JSONResponse({"job_id": job_id, **_get_reservados_index_job(job_id)})


@app.get("/admin/reservados/reindexar/estado/{job_id}")
def admin_reservados_reindexar_estado(job_id: str, request: Request, user: dict = Depends(require_admin)):
    job = _get_reservados_index_job(job_id)
    if not job:
        return JSONResponse({"estado": "no_encontrado", "mensaje": "Tarea no encontrada."}, status_code=404)
    return JSONResponse({"job_id": job_id, **job})


# ── RESERVADOS — BÚSQUEDA ──────────────────────────────────────────

@app.get("/reservados", response_class=HTMLResponse)
def reservados_get(request: Request, user: dict = Depends(require_reservados)):
    return templates.TemplateResponse("reservados_buscar.html", {"request": request, "user": user})


@app.post("/reservados")
def reservados_post(
    request: Request,
    q: str = Form(""),
    campo: str = Form("todo"),
    fecha_desde: str = Form(""),
    fecha_hasta: str = Form(""),
    paginas_min: int = Form(0),
    paginas_max: int = Form(0),
    user: dict = Depends(require_reservados),
    _csrf: None = Depends(verificar_csrf),
):
    require_password_ok(request, user)
    q = (q or "").strip()
    hay_filtros = bool((campo and campo != "todo") or fecha_desde or fecha_hasta or paginas_min or paginas_max)
    if not q and not hay_filtros:
        return RedirectResponse("/reservados", status_code=302)
    params: dict[str, str] = {"q": q, "page": "1"}
    if campo and campo != "todo":
        params["campo"] = campo
    if fecha_desde:
        params["fecha_desde"] = fecha_desde
    if fecha_hasta:
        params["fecha_hasta"] = fecha_hasta
    if paginas_min:
        params["paginas_min"] = str(paginas_min)
    if paginas_max:
        params["paginas_max"] = str(paginas_max)
    return RedirectResponse(f"/reservados/resultados?{urlencode(params)}", status_code=302)


@app.get("/reservados/resultados", response_class=HTMLResponse)
def reservados_resultados_get(
    request: Request,
    user: dict = Depends(require_reservados),
    q: str = Query(""),
    page: int = Query(1),
    campo: str = Query("todo"),
    fecha_desde: str = Query(""),
    fecha_hasta: str = Query(""),
    paginas_min: int = Query(0),
    paginas_max: int = Query(0),
):
    require_password_ok(request, user)
    q = (q or "").strip()
    hay_filtros = bool((campo and campo != "todo") or fecha_desde or fecha_hasta or paginas_min or paginas_max)
    if not q and not hay_filtros:
        return RedirectResponse("/reservados", status_code=302)

    page_size = 20
    page = max(1, int(page or 1))

    todos = list(buscar_memorandos(
        q,
        limit=5000,
        campo=campo or "todo",
        fecha_desde=fecha_desde or "",
        fecha_hasta=fecha_hasta or "",
        paginas_min=paginas_min or 0,
        paginas_max=paginas_max or 0,
        tabla="reservados",
    ))

    total = len(todos)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    resultados = todos[start:start + page_size]

    if page == 1:
        try:
            with get_db() as conn:
                registrar_busqueda(conn, user["id"], q, total, client_ip(request))
                registrar_auditoria(conn, "RESERVADO_BUSQUEDA_REALIZADA", usuario_id=user["id"], detalle={"q": q, "cantidad_resultados": total, "paginacion": True}, ip=client_ip(request), equipo=ua(request), resultado="OK")
        except Exception:
            pass

    filtros_params: dict[str, str] = {}
    if campo and campo != "todo":
        filtros_params["campo"] = campo
    if fecha_desde:
        filtros_params["fecha_desde"] = fecha_desde
    if fecha_hasta:
        filtros_params["fecha_hasta"] = fecha_hasta
    if paginas_min:
        filtros_params["paginas_min"] = str(paginas_min)
    if paginas_max:
        filtros_params["paginas_max"] = str(paginas_max)
    filtros_url = ("&" + urlencode(filtros_params)) if filtros_params else ""

    return templates.TemplateResponse("reservados_resultados.html", {
        "request": request, "user": user, "q": q,
        "resultados": resultados, "total": total, "page": page,
        "page_size": page_size, "total_pages": total_pages,
        "page_numbers": _sigemep_page_window(page, total_pages),
        "campo": campo or "todo",
        "fecha_desde": fecha_desde or "",
        "fecha_hasta": fecha_hasta or "",
        "paginas_min": paginas_min or 0,
        "paginas_max": paginas_max or 0,
        "filtros_url": filtros_url,
        "filtros_activos": bool(filtros_params),
    })


# ── RESERVADOS — VISOR SEGURO ──────────────────────────────────────

@app.get("/reservados/visor/{mem_id}", response_class=HTMLResponse)
def reservados_visor(mem_id: int, request: Request, user: dict = Depends(require_reservados), pagina: int = Query(1)):
    require_password_ok(request, user)
    with get_db() as conn:
        m = conn.execute("SELECT * FROM reservados WHERE id = ? AND activo = 1", (mem_id,)).fetchone()
        if not m:
            raise HTTPException(404)
        total_paginas = m["cantidad_paginas"] or 1
        pagina = max(1, min(pagina, total_paginas))
        registrar_auditoria(
            conn,
            "RESERVADO_VISOR_ABIERTO",
            usuario_id=user["id"],
            memorando_id=mem_id,
            detalle={"archivo": m["nombre_archivo"]},
            ip=client_ip(request),
            equipo=ua(request),
            resultado="OK",
        )
        memorando = dict(m)
    return templates.TemplateResponse("reservados_visor.html", {
        "request": request,
        "user": user,
        "memorando": memorando,
        "pagina": pagina,
        "total_paginas": total_paginas,
    })


@app.get("/reservados/visor/{mem_id}/imagen/{n}")
def reservados_visor_imagen(mem_id: int, n: int, request: Request, user: dict = Depends(require_reservados)):
    with get_db() as conn:
        m = conn.execute("SELECT * FROM reservados WHERE id = ? AND activo = 1", (mem_id,)).fetchone()
        if not m:
            raise HTTPException(404)
        total_paginas = m["cantidad_paginas"] or 1
        if n < 1 or n > total_paginas:
            raise HTTPException(404)
        ruta = ruta_absoluta_segura_reservados(m["ruta_archivo"], conn)
        if not ruta:
            raise HTTPException(404)
        preview = Path(m["primera_hoja_img"]) if m["primera_hoja_img"] else None
        img_bytes = imagen_pagina_con_marca(ruta, n - 1, preview, user["usuario"], user["rol"], client_ip(request))
    return Response(img_bytes, media_type="image/png")


@app.get("/admin/auditoria", response_class=HTMLResponse)
def admin_auditoria(request: Request, user: dict = Depends(require_admin), filtro_usuario: Optional[int] = Query(None), filtro_accion: Optional[str] = Query(None), desde: Optional[str] = Query(None), hasta: Optional[str] = Query(None), filtro_memorando: Optional[int] = Query(None), filtro_resultado: Optional[str] = Query(None)):
    with get_db() as conn:
        usuarios_opts = conn.execute("SELECT id, usuario FROM usuarios ORDER BY usuario").fetchall()
        acciones = conn.execute("SELECT DISTINCT accion FROM auditoria ORDER BY accion").fetchall()
    rows = listar_auditoria(usuario_id=filtro_usuario, accion=filtro_accion, fecha_desde=desde, fecha_hasta=hasta, memorando_id=filtro_memorando, resultado=filtro_resultado, limit=800)
    export_params = {k: v for k, v in {"filtro_usuario": filtro_usuario, "filtro_accion": filtro_accion, "desde": desde, "hasta": hasta, "filtro_memorando": filtro_memorando, "filtro_resultado": filtro_resultado}.items() if v}
    export_url = "/admin/auditoria/export.csv" + ("?" + urlencode(export_params) if export_params else "")
    return templates.TemplateResponse("auditoria.html", {"request": request, "user": user, "rows": rows, "usuarios_opts": [dict(r) for r in usuarios_opts], "acciones_opts": [r[0] for r in acciones], "filtro_usuario": filtro_usuario, "filtro_accion": filtro_accion, "desde": desde, "hasta": hasta, "filtro_memorando": filtro_memorando, "filtro_resultado": filtro_resultado, "export_url": export_url})


@app.get("/admin/auditoria/export.csv")
def admin_auditoria_export(request: Request, user: dict = Depends(require_admin), filtro_usuario: Optional[int] = Query(None), filtro_accion: Optional[str] = Query(None), desde: Optional[str] = Query(None), hasta: Optional[str] = Query(None), filtro_memorando: Optional[int] = Query(None), filtro_resultado: Optional[str] = Query(None)):
    rows = listar_auditoria(usuario_id=filtro_usuario, accion=filtro_accion, fecha_desde=desde, fecha_hasta=hasta, memorando_id=filtro_memorando, resultado=filtro_resultado, limit=5000)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["id", "fecha_hora", "usuario", "accion", "memorando_id", "ip", "resultado", "detalle"])
    for r in rows:
        writer.writerow([r.get("id"), r.get("fecha_hora"), r.get("usuario_nombre") or r.get("usuario_id") or "", r.get("accion"), r.get("memorando_id") or "", r.get("ip") or "", r.get("resultado") or "", r.get("detalle") or ""])
    return Response(content=output.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="auditoria_sigemep.csv"'})


@app.get("/admin/ingresos", response_class=HTMLResponse)
def admin_ingresos(request: Request, user: dict = Depends(require_admin)):
    rows = listar_auditoria(accion=None, limit=1200)
    rows = [r for r in rows if r.get("accion") in ("LOGIN_EXITOSO", "LOGIN_FALLIDO", "LOGOUT")]
    return templates.TemplateResponse("ingresos.html", {"request": request, "user": user, "rows": rows})


@app.get("/brigada/nuevo_memorando", response_class=HTMLResponse)
def nuevo_memorando_form(request: Request, user: dict = Depends(require_roles("BRIGADA"))):
    hoy = datetime.today().strftime("%Y-%m-%d")
    anio = datetime.today().year
    return templates.TemplateResponse("nuevo_memorando.html", {
        "request": request, "user": user, "hoy": hoy, "anio": anio,
    })


@app.post("/brigada/nuevo_memorando/preview")
async def nuevo_memorando_preview(
    request: Request,
    user: dict = Depends(require_roles("BRIGADA")),
    nro: str = Form(...),
    anio: str = Form(...),
    iniciales: str = Form(""),
    fecha_memo: str = Form(...),
    de: str = Form(...),
    a: str = Form(...),
    hecho: str = Form(...),
    tipo_fecha: str = Form(...),
    fecha_hecho: str = Form(...),
    hora: str = Form(""),
    lugar: str = Form(...),
    etiqueta_persona: str = Form(...),
    persona: str = Form(""),
    imputado: str = Form(""),
    elementos_sustraidos: str = Form("No hubo."),
    elementos_secuestrados: str = Form("No hubo."),
    dependencia: str = Form(""),
    magistrado: str = Form(""),
    resena: str = Form(...),
    _csrf: None = Depends(verificar_csrf),
):
    import fitz as _fitz
    campos = {
        "nro": nro, "anio": anio, "iniciales": iniciales,
        "fecha_memo": fecha_memo, "de": de, "a": a, "hecho": hecho,
        "tipo_fecha": tipo_fecha, "fecha_hecho": fecha_hecho, "hora": hora,
        "lugar": lugar, "etiqueta_persona": etiqueta_persona, "persona": persona,
        "imputado": imputado, "elementos_sustraidos": elementos_sustraidos,
        "elementos_secuestrados": elementos_secuestrados,
        "dependencia": dependencia, "magistrado": magistrado, "resena": resena,
    }
    pdf_bytes = generar_pdf_memorando(campos)
    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(matrix=_fitz.Matrix(1.5, 1.5), alpha=False)
    doc.close()
    png_b64 = base64.b64encode(pix.tobytes("png")).decode()
    nombre = nombre_archivo_memorando(campos)
    return JSONResponse({"png_b64": png_b64, "nombre": nombre})


@app.post("/brigada/nuevo_memorando/guardar")
async def nuevo_memorando_guardar(
    request: Request,
    user: dict = Depends(require_roles("BRIGADA")),
    nro: str = Form(...),
    anio: str = Form(...),
    iniciales: str = Form(""),
    fecha_memo: str = Form(...),
    de: str = Form(...),
    a: str = Form(...),
    hecho: str = Form(...),
    tipo_fecha: str = Form(...),
    fecha_hecho: str = Form(...),
    hora: str = Form(""),
    lugar: str = Form(...),
    etiqueta_persona: str = Form(...),
    persona: str = Form(""),
    imputado: str = Form(""),
    elementos_sustraidos: str = Form("No hubo."),
    elementos_secuestrados: str = Form("No hubo."),
    dependencia: str = Form(""),
    magistrado: str = Form(""),
    resena: str = Form(...),
    _csrf: None = Depends(verificar_csrf),
):
    campos = {
        "nro": nro, "anio": anio, "iniciales": iniciales,
        "fecha_memo": fecha_memo, "de": de, "a": a, "hecho": hecho,
        "tipo_fecha": tipo_fecha, "fecha_hecho": fecha_hecho, "hora": hora,
        "lugar": lugar, "etiqueta_persona": etiqueta_persona, "persona": persona,
        "imputado": imputado, "elementos_sustraidos": elementos_sustraidos,
        "elementos_secuestrados": elementos_secuestrados,
        "dependencia": dependencia, "magistrado": magistrado, "resena": resena,
    }
    nombre = nombre_archivo_memorando(campos)
    with get_db() as conn:
        carpeta = carpeta_pdf_actual(conn)
    if not carpeta or not Path(carpeta).is_dir():
        raise HTTPException(status_code=500,
            detail="La carpeta de destino no está disponible. Contactá al administrador.")
    carpeta_path = Path(carpeta)
    destino = carpeta_path / nombre
    if destino.exists():
        raise HTTPException(status_code=409,
            detail=f"Ya existe un memorando con ese número y fecha. Verificá el correlativo.")
    try:
        destino.resolve().relative_to(carpeta_path.resolve())
    except ValueError:
        raise HTTPException(400, detail="Número de memorando inválido.")
    pdf_bytes = generar_pdf_memorando(campos)
    destino.write_bytes(pdf_bytes)
    with get_db() as conn:
        registrar_auditoria(
            conn, "MEMORANDO_CREADO",
            usuario_id=user["id"],
            detalle={"nombre_archivo": nombre, "carpeta": str(carpeta)},
            ip=request.client.host if request.client else "desconocida",
            equipo=request.headers.get("user-agent", ""),
            resultado="OK",
        )
    return RedirectResponse(f"/dashboard/brigada?memo_creado={nombre}", status_code=302)


@app.get("/acceso_denegado", response_class=HTMLResponse)
def acceso_denegado(request: Request, user: dict = Depends(require_login)):
    return templates.TemplateResponse("acceso_denegado.html", {"request": request, "user": user})


@app.get("/error", response_class=HTMLResponse)
def error_page(request: Request):
    return templates.TemplateResponse("error.html", {"request": request, "mensaje": "Error."})


# ── INSERTAR MEMORANDO (BRIGADA) ──────────────────────────────────

@app.get("/brigada/insertar_memorando", response_class=HTMLResponse)
def insertar_memorando_get(request: Request, user: dict = Depends(require_roles("BRIGADA"))):
    require_password_ok(request, user)
    return templates.TemplateResponse("insertar_memorando.html", {
        "request": request, "user": user,
        "tiene_permiso_reservados": bool(user.get("permiso_reservados")),
    })


@app.post("/brigada/insertar_memorando/verificar_hash")
def insertar_memorando_verificar_hash(
    request: Request,
    user: dict = Depends(require_roles("BRIGADA")),
    hash_sha256: str = Form(...),
    _csrf: None = Depends(verificar_csrf),
):
    require_password_ok(request, user)
    h = hash_sha256.strip().lower()
    if len(h) != 64 or not all(c in "0123456789abcdef" for c in h):
        return JSONResponse({"error": "Hash inválido."}, status_code=400)
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT m.nombre_archivo, m.fecha_indexado, u.usuario
            FROM memorandos m
            LEFT JOIN usuarios u ON u.id = m.usuario_id
            WHERE m.hash_sha256 = ? AND m.activo = 1
            LIMIT 1
            """,
            (h,),
        ).fetchone()
    if row:
        return JSONResponse({
            "existe": True,
            "nombre_archivo": row["nombre_archivo"],
            "fecha_subida": row["fecha_indexado"],
            "usuario": row["usuario"] or "—",
        })
    return JSONResponse({"existe": False})


def _sanitizar_nombre_subida(nombre: str, default: str = "memorando.pdf") -> str:
    """Descarta cualquier componente de directorio del nombre subido por el
    cliente (p. ej. '../../etc/x.pdf' o 'a\\..\\..\\x.pdf'), quedándose solo
    con el nombre de archivo final, para evitar escribir fuera de la carpeta
    de destino."""
    nombre = (nombre or "").strip().replace("\\", "/")
    nombre = nombre.split("/")[-1].strip()
    if not nombre or nombre in (".", ".."):
        return default
    return nombre


async def _leer_archivo_con_limite(archivo: UploadFile, limite_bytes: int) -> bytes:
    """Lee el UploadFile en bloques, abortando apenas se supera el límite,
    para no acumular en memoria un archivo arbitrariamente grande."""
    partes: list[bytes] = []
    total = 0
    while True:
        bloque = await archivo.read(1024 * 1024)
        if not bloque:
            break
        total += len(bloque)
        if total > limite_bytes:
            raise HTTPException(413, detail=f"El archivo supera el límite de {MAX_UPLOAD_MB} MB.")
        partes.append(bloque)
    return b"".join(partes)


@app.post("/brigada/insertar_memorando/verificar_archivo")
async def insertar_memorando_verificar_archivo(
    request: Request,
    user: dict = Depends(require_roles("BRIGADA")),
    archivo: UploadFile = File(...),
    tipo: str = Form("memorando"),
    _csrf: None = Depends(verificar_csrf),
):
    require_password_ok(request, user)
    tipo = tipo if tipo in ("memorando", "reservado") else "memorando"
    if tipo == "reservado" and not user.get("permiso_reservados"):
        return JSONResponse({"error": "Sin permiso para subir reservados."}, status_code=403)
    contenido = await _leer_archivo_con_limite(archivo, MAX_UPLOAD_BYTES)
    if len(contenido) < 5:
        return JSONResponse({"error": "El archivo está vacío o no es un PDF válido."}, status_code=400)
    hash_sha256 = hashlib.sha256(contenido).hexdigest()
    nombre_intentado = (archivo.filename or "").strip()[:255]
    nombre_sanitizado = _sanitizar_nombre_subida(archivo.filename or "memorando.pdf")
    nombre_duplicado = False
    with get_db() as conn:
        if tipo == "reservado":
            row = conn.execute(
                "SELECT nombre_archivo, fecha_indexado FROM reservados WHERE hash_sha256 = ? AND activo = 1 LIMIT 1",
                (hash_sha256,),
            ).fetchone()
            usuario_dup = None
        else:
            row = conn.execute(
                """
                SELECT m.nombre_archivo, m.fecha_indexado, u.usuario
                FROM memorandos m
                LEFT JOIN usuarios u ON u.id = m.usuario_id
                WHERE m.hash_sha256 = ? AND m.activo = 1
                LIMIT 1
                """,
                (hash_sha256,),
            ).fetchone()
            usuario_dup = row["usuario"] if row else None
        if row:
            ya_existe_alerta = conn.execute(
                "SELECT id FROM alertas_revision WHERE hash_sha256 = ? AND usuario_id = ? AND estado = 'pendiente' LIMIT 1",
                (hash_sha256, user["id"]),
            ).fetchone()
            if not ya_existe_alerta:
                conn.execute(
                    "INSERT INTO alertas_revision (hash_sha256, nombre_archivo, nombre_existente, usuario_id, mensaje, tipo) VALUES (?, ?, ?, ?, ?, 'hash')",
                    (hash_sha256, nombre_intentado, row["nombre_archivo"], user["id"], f"Intento de subida detectado automáticamente como duplicado ({tipo})."),
                )
                registrar_auditoria(
                    conn, "ALERTA_DUPLICADO_AUTOMATICA",
                    usuario_id=user["id"],
                    detalle={"hash": hash_sha256[:16] + "...", "tipo": tipo, "nombre_intentado": nombre_intentado, "nombre_existente": row["nombre_archivo"]},
                    ip=client_ip(request), equipo=ua(request), resultado="OK",
                )
        elif nombre_sanitizado:
            carpeta = carpeta_reservados_actual(conn) if tipo == "reservado" else carpeta_pdf_actual(conn)
            if (Path(str(carpeta)) / nombre_sanitizado).is_file():
                nombre_duplicado = True
                ya_existe_alerta_nombre = conn.execute(
                    "SELECT id FROM alertas_revision WHERE nombre_archivo = ? AND usuario_id = ? AND estado = 'pendiente' AND tipo = 'nombre' LIMIT 1",
                    (nombre_sanitizado, user["id"]),
                ).fetchone()
                if not ya_existe_alerta_nombre:
                    conn.execute(
                        "INSERT INTO alertas_revision (hash_sha256, nombre_archivo, nombre_existente, usuario_id, mensaje, tipo) VALUES (?, ?, ?, ?, ?, 'nombre')",
                        (hash_sha256, nombre_intentado, nombre_sanitizado, user["id"], f"Nombre de archivo ya existente pero contenido distinto ({tipo})."),
                    )
                    registrar_auditoria(
                        conn, "ALERTA_NOMBRE_DUPLICADO_AUTOMATICA",
                        usuario_id=user["id"],
                        detalle={"tipo": tipo, "nombre_intentado": nombre_intentado, "nombre_existente": nombre_sanitizado},
                        ip=client_ip(request), equipo=ua(request), resultado="OK",
                    )
    if row:
        return JSONResponse({
            "existe": True,
            "hash": hash_sha256,
            "nombre_archivo": row["nombre_archivo"],
            "fecha_subida": row["fecha_indexado"],
            "usuario": usuario_dup or "—",
        })
    if nombre_duplicado:
        return JSONResponse({
            "existe": False,
            "nombre_duplicado": True,
            "nombre_existente": nombre_sanitizado,
            "hash": hash_sha256,
        })
    return JSONResponse({"existe": False, "hash": hash_sha256})


@app.post("/brigada/insertar_memorando/guardar")
async def insertar_memorando_guardar(
    request: Request,
    user: dict = Depends(require_roles("BRIGADA")),
    archivo: UploadFile = File(...),
    tipo: str = Form("memorando"),
    _csrf: None = Depends(verificar_csrf),
):
    require_password_ok(request, user)
    tipo = tipo if tipo in ("memorando", "reservado") else "memorando"
    if tipo == "reservado" and not user.get("permiso_reservados"):
        raise HTTPException(403, detail="Sin permiso para subir reservados.")

    nombre_original = _sanitizar_nombre_subida(archivo.filename or "memorando.pdf")
    if not nombre_original.lower().endswith(".pdf"):
        raise HTTPException(400, detail="Solo se aceptan archivos PDF.")

    contenido = await _leer_archivo_con_limite(archivo, MAX_UPLOAD_BYTES)
    if len(contenido) < 5:
        raise HTTPException(400, detail="El archivo está vacío o no es un PDF válido.")

    hash_sha256 = hashlib.sha256(contenido).hexdigest()
    tabla_dup = "reservados" if tipo == "reservado" else "memorandos"

    with get_db() as conn:
        dup = conn.execute(
            f"SELECT nombre_archivo FROM {tabla_dup} WHERE hash_sha256 = ? AND activo = 1 LIMIT 1",
            (hash_sha256,),
        ).fetchone()
        if dup:
            ya_existe_alerta = conn.execute(
                "SELECT id FROM alertas_revision WHERE hash_sha256 = ? AND usuario_id = ? AND estado = 'pendiente' LIMIT 1",
                (hash_sha256, user["id"]),
            ).fetchone()
            if not ya_existe_alerta:
                conn.execute(
                    "INSERT INTO alertas_revision (hash_sha256, nombre_archivo, nombre_existente, usuario_id, mensaje, tipo) VALUES (?, ?, ?, ?, ?, 'hash')",
                    (hash_sha256, nombre_original[:255], dup["nombre_archivo"], user["id"], f"Intento de subida detectado automáticamente como duplicado ({tipo})."),
                )
                registrar_auditoria(
                    conn, "ALERTA_DUPLICADO_AUTOMATICA",
                    usuario_id=user["id"],
                    detalle={"hash": hash_sha256[:16] + "...", "tipo": tipo, "nombre_intentado": nombre_original, "nombre_existente": dup["nombre_archivo"]},
                    ip=client_ip(request), equipo=ua(request), resultado="OK",
                )
            return JSONResponse(
                {"error": "duplicado", "detail": "El archivo ya fue subido anteriormente.",
                 "nombre_existente": dup["nombre_archivo"]},
                status_code=409,
            )
        carpeta = carpeta_reservados_actual(conn) if tipo == "reservado" else carpeta_pdf_actual(conn)

    carpeta_path = Path(str(carpeta))
    if not carpeta_path.is_dir():
        raise HTTPException(500, detail="La carpeta de destino no está disponible. Contactá al administrador.")

    destino = carpeta_path / nombre_original
    nombre_duplicado = destino.is_file()
    if nombre_duplicado:
        with get_db() as conn:
            ya_existe_alerta_nombre = conn.execute(
                "SELECT id FROM alertas_revision WHERE nombre_archivo = ? AND usuario_id = ? AND estado = 'pendiente' AND tipo = 'nombre' LIMIT 1",
                (nombre_original, user["id"]),
            ).fetchone()
            if not ya_existe_alerta_nombre:
                conn.execute(
                    "INSERT INTO alertas_revision (hash_sha256, nombre_archivo, nombre_existente, usuario_id, mensaje, tipo) VALUES (?, ?, ?, ?, ?, 'nombre')",
                    (hash_sha256, nombre_original, nombre_original, user["id"], f"Se guardó un archivo con nombre ya existente pero contenido distinto ({tipo})."),
                )
                registrar_auditoria(
                    conn, "ALERTA_NOMBRE_DUPLICADO_AUTOMATICA",
                    usuario_id=user["id"],
                    detalle={"tipo": tipo, "nombre": nombre_original},
                    ip=client_ip(request), equipo=ua(request), resultado="OK",
                )

    if destino.exists():
        stem, suf = Path(nombre_original).stem, Path(nombre_original).suffix
        i = 1
        while destino.exists():
            destino = carpeta_path / f"{stem}_{i}{suf}"
            i += 1

    try:
        destino.resolve().relative_to(carpeta_path.resolve())
    except ValueError:
        raise HTTPException(400, detail="Nombre de archivo inválido.")

    destino.write_bytes(contenido)

    texto, n_pages = "", 0
    preview_path: Optional[Path] = None
    try:
        import fitz as _fitz
        doc = _fitz.open(stream=contenido, filetype="pdf")
        parts = [doc.load_page(i).get_text("text") or "" for i in range(len(doc))]
        n_pages = len(doc)
        meta = doc.metadata or {}
        doc.close()
        meta_lines = [f"{k}: {v}" for k, v in (meta or {}).items() if v]
        texto = "\n".join(parts)
        if meta_lines:
            texto += "\n" + "\n".join(meta_lines)
        preview_name = f"m_{hashlib.md5(hash_sha256.encode()).hexdigest()[:16]}.png"
        preview_path = PREVIEWS_DIR / preview_name
        renderizar_primera_hoja_base(destino, preview_path)
    except Exception as exc:
        logger.warning("Proceso PDF fallido para %s: %s", destino.name, exc)

    try:
        rel = destino.resolve().relative_to(carpeta_path.resolve()).as_posix()
    except ValueError:
        rel = destino.name

    with get_db() as conn:
        if tipo == "reservado":
            conn.execute(
                """
                INSERT INTO reservados (
                    nombre_archivo, ruta_archivo, texto_extraido, cantidad_paginas,
                    primera_hoja_img, activo, tamanio_bytes, mtime, hash_sha256, usuario_id
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    destino.name, rel, texto, n_pages,
                    str(preview_path) if preview_path else None,
                    len(contenido), int(destino.stat().st_mtime),
                    hash_sha256, user["id"],
                ),
            )
            try:
                rebuild_fts(conn, "reservados_fts")
            except Exception:
                pass
            registrar_auditoria(
                conn, "RESERVADO_SUBIDO",
                usuario_id=user["id"],
                detalle={"nombre": destino.name, "paginas": n_pages, "bytes": len(contenido)},
                ip=client_ip(request), equipo=ua(request), resultado="OK",
            )
        else:
            conn.execute(
                """
                INSERT INTO memorandos (
                    nombre_archivo, ruta_archivo, texto_extraido, cantidad_paginas,
                    primera_hoja_img, activo, tamanio_bytes, mtime, fecha_hecho,
                    hash_sha256, usuario_id
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    destino.name, rel, texto, n_pages,
                    str(preview_path) if preview_path else None,
                    len(contenido), int(destino.stat().st_mtime),
                    extraer_fecha_hecho(destino.name),
                    hash_sha256, user["id"],
                ),
            )
            try:
                rebuild_fts(conn)
            except Exception:
                pass
            registrar_auditoria(
                conn, "MEMORANDO_SUBIDO",
                usuario_id=user["id"],
                detalle={"nombre": destino.name, "paginas": n_pages, "bytes": len(contenido)},
                ip=client_ip(request), equipo=ua(request), resultado="OK",
            )

    return JSONResponse({
        "ok": True, "nombre": destino.name, "paginas": n_pages,
        "nombre_duplicado": nombre_duplicado,
    })


@app.post("/brigada/insertar_memorando/avisar_admin")
def insertar_memorando_avisar_admin(
    request: Request,
    user: dict = Depends(require_roles("BRIGADA")),
    hash_sha256: str = Form(...),
    nombre_archivo: str = Form(""),
    _csrf: None = Depends(verificar_csrf),
):
    require_password_ok(request, user)
    return JSONResponse({"ok": True, "mensaje": "El administrador ya fue notificado automáticamente."})


# ── ALERTAS DE REVISIÓN (ADMIN) ──────────────────────────────────

@app.get("/admin/alertas_revision", response_class=HTMLResponse)
def admin_alertas_revision(
    request: Request,
    user: dict = Depends(require_admin),
    estado: Optional[str] = Query(None),
):
    flash = request.session.pop("sigemep_flash", None)
    sql = """
        SELECT ar.id, ar.hash_sha256, ar.nombre_archivo, ar.nombre_existente,
               ar.fecha_alerta, ar.estado, ar.mensaje, ar.tipo, u.usuario, u.nombre_apellido
        FROM alertas_revision ar
        JOIN usuarios u ON u.id = ar.usuario_id
    """
    params: list[Any] = []
    if estado in ("pendiente", "revisado"):
        sql += " WHERE ar.estado = ?"
        params.append(estado)
    sql += " ORDER BY ar.fecha_alerta DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        n_pendientes = conn.execute(
            "SELECT COUNT(*) FROM alertas_revision WHERE estado = 'pendiente'"
        ).fetchone()[0]
        ultimo_subido_row = conn.execute("""
            SELECT a.fecha_hora, a.detalle, u.usuario, u.nombre_apellido
            FROM auditoria a
            JOIN usuarios u ON u.id = a.usuario_id
            WHERE a.accion = 'MEMORANDO_SUBIDO'
            ORDER BY a.fecha_hora DESC
            LIMIT 1
        """).fetchone()

    ultimo_subido = None
    if ultimo_subido_row:
        try:
            detalle = json.loads(ultimo_subido_row["detalle"] or "{}")
        except (ValueError, TypeError):
            detalle = {}
        ultimo_subido = {
            "nombre_archivo": detalle.get("nombre", "—"),
            "usuario": ultimo_subido_row["usuario"],
            "nombre_apellido": ultimo_subido_row["nombre_apellido"],
            "fecha_hora": ultimo_subido_row["fecha_hora"],
        }

    return templates.TemplateResponse("alertas_revision.html", {
        "request": request, "user": user,
        "alertas": [dict(r) for r in rows],
        "n_pendientes": n_pendientes,
        "filtro_estado": estado or "",
        "flash": flash,
        "ultimo_subido": ultimo_subido,
    })


@app.get("/api/alertas_pendientes")
def api_alertas_pendientes(user: dict = Depends(require_admin)):
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM alertas_revision WHERE estado = 'pendiente'"
        ).fetchone()[0]
    return {"n": n}


@app.post("/admin/alertas_revision/{alerta_id}/marcar_revisado")
def admin_marcar_alerta_revisado(
    alerta_id: int, request: Request, user: dict = Depends(require_admin),
    _csrf: None = Depends(verificar_csrf),
):
    with get_db() as conn:
        alerta = conn.execute(
            "SELECT id FROM alertas_revision WHERE id = ?", (alerta_id,)
        ).fetchone()
        if not alerta:
            raise HTTPException(404)
        conn.execute(
            "UPDATE alertas_revision SET estado = 'revisado' WHERE id = ?", (alerta_id,)
        )
        registrar_auditoria(
            conn, "ALERTA_MARCADA_REVISADA",
            usuario_id=user["id"],
            detalle={"alerta_id": alerta_id},
            ip=client_ip(request), equipo=ua(request), resultado="OK",
        )
    request.session["sigemep_flash"] = "Alerta marcada como revisada."
    return RedirectResponse("/admin/alertas_revision", status_code=302)
