"""SIGEMEP - Sistema de Consulta y Auditoría de Memorandos."""
import base64
import csv
import io
import logging
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import BASE_DIR, SESSION_SECRET
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
from services.db import get_config, get_db, init_db, set_config
from services.pdf_service import (
    carpeta_pdf_actual,
    imagen_primera_hoja_con_marca,
    indexar_memorandos,
    ruta_absoluta_segura,
)
from services.search_service import buscar_memorandos
from services.memo_creator import generar_pdf_memorando, nombre_archivo_memorando

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sigemep")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="SIGEMEP", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

ROLES_CONSULTA_MEMORANDOS = frozenset({"ADMIN", "JEFE", "BRIGADA"})
ROLES_DESCARGA_PDF = frozenset({"ADMIN", "JEFE"})

INDEX_JOBS: dict[str, dict[str, Any]] = {}
INDEX_JOBS_LOCK = threading.Lock()


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
def login_post(request: Request, usuario: str = Form(...), password: str = Form(...)):
    ip = client_ip(request)
    equipo = ua(request)
    with get_db() as conn:
        row = obtener_usuario_por_login(conn, usuario)
        if not row or not verificar_password(password, row["password_hash"]):
            registrar_auditoria(conn, "LOGIN_FALLIDO", usuario_id=row["id"] if row else None, detalle={"usuario_ingresado": usuario.strip()}, ip=ip, equipo=equipo, resultado="FALLIDO")
            return templates.TemplateResponse("login.html", {"request": request, "error": "Usuario o contraseña incorrectos."}, status_code=401)
        u = dict(row)
        if u["estado"] != "ACTIVO":
            registrar_auditoria(conn, "LOGIN_FALLIDO", usuario_id=u["id"], detalle={"motivo": f"Estado {u['estado']}"}, ip=ip, equipo=equipo, resultado="DENEGADO")
            return templates.TemplateResponse("login.html", {"request": request, "error": "Su cuenta no está activa. Contacte al administrador."}, status_code=403)
        conn.execute("UPDATE usuarios SET ultimo_login = CURRENT_TIMESTAMP WHERE id = ?", (u["id"],))
        registrar_auditoria(conn, "LOGIN_EXITOSO", usuario_id=u["id"], ip=ip, equipo=equipo, resultado="OK")
    request.session["user_id"] = u["id"]
    request.session["usuario"] = u["usuario"]
    request.session["rol"] = u["rol"]
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
async def cambiar_password_post(request: Request, user: dict = Depends(require_login)):
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


def ruta_absoluta_segura(ruta_guardada, conn=None):
    from pathlib import Path

    base_dir = Path(__file__).resolve().parent
    raw = str(ruta_guardada or "").strip().strip(chr(34)).strip(chr(39))

    if not raw:
        return None

    posibles_dirs = []

    def add_dir(value):
        if value is None:
            return

        s = str(value).strip().strip(chr(34)).strip(chr(39))
        if not s:
            return

        p = Path(s)
        if not p.is_absolute():
            p = base_dir / p

        try:
            p = p.resolve()
        except Exception:
            pass

        if p.exists() and p.is_dir() and p not in posibles_dirs:
            posibles_dirs.append(p)

    # 1) Carpetas guardadas en tabla configuracion.
    if conn is not None:
        try:
            rows = conn.execute("SELECT * FROM configuracion").fetchall()

            for row in rows:
                try:
                    data = dict(row)
                except Exception:
                    continue

                # Buscar columnas tipo carpeta/ruta/pdf/path/directorio.
                for k, v in data.items():
                    lk = str(k).lower()
                    if ("carpeta" in lk) or ("ruta" in lk) or ("pdf" in lk) or ("directorio" in lk) or ("path" in lk):
                        add_dir(v)

                # Si la tabla es clave/valor, tomar el valor cuando la clave habla de PDF.
                vals = list(data.values())
                if len(vals) >= 2:
                    key_text = str(vals[0]).lower()
                    if ("carpeta" in key_text) or ("ruta" in key_text) or ("pdf" in key_text) or ("directorio" in key_text) or ("path" in key_text):
                        add_dir(vals[1])
        except Exception:
            pass

    # 2) Carpetas comunes dentro de C:\SIGEMEP_APP.
    for d in (
        base_dir,
        base_dir / "pdfs",
        base_dir / "PDFS",
        base_dir / "memorandos",
        base_dir / "Memorandos",
        base_dir / "uploads",
        base_dir / "archivos",
        base_dir / "archivos_pdf",
        base_dir / "data",
        base_dir / "static" / "pdfs",
    ):
        add_dir(d)

    original = Path(raw)

    # 3) Ruta absoluta.
    if original.is_absolute():
        try:
            original_resuelta = original.resolve()
        except Exception:
            original_resuelta = original

        if original_resuelta.exists() and original_resuelta.is_file():
            return str(original_resuelta)

    # 4) Ruta relativa.
    relativo_base = base_dir / raw
    if relativo_base.exists() and relativo_base.is_file():
        return str(relativo_base.resolve())

    # 5) Solo nombre de archivo.
    nombre = Path(raw).name

    for carpeta in posibles_dirs:
        candidato = carpeta / raw
        if candidato.exists() and candidato.is_file():
            return str(candidato.resolve())

        candidato = carpeta / nombre
        if candidato.exists() and candidato.is_file():
            return str(candidato.resolve())

    # 6) Búsqueda recursiva por nombre exacto dentro de carpetas candidatas.
    for carpeta in posibles_dirs:
        try:
            for candidato in carpeta.rglob(nombre):
                if candidato.exists() and candidato.is_file():
                    return str(candidato.resolve())
        except Exception:
            continue

    return None

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
def admin_usuarios(request: Request, user: dict = Depends(require_admin), q: str = "", rol: str = "", estado: str = ""):
    flash = request.session.pop("sigemep_flash", None)
    err = request.query_params.get("error")
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
    return templates.TemplateResponse("usuarios.html", {"request": request, "user": user, "usuarios": [dict(r) for r in rows], "flash": flash, "query_error": err, "q": q, "rol": rol, "estado": estado})


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
def admin_aprobar(request: Request, uid: int, user: dict = Depends(require_admin)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row and row["estado"] == "PENDIENTE":
            conn.execute("UPDATE usuarios SET estado = 'ACTIVO', aprobado_por = ?, aprobado_en = CURRENT_TIMESTAMP WHERE id = ?", (user["id"], uid))
            registrar_auditoria(conn, "USUARIO_APROBADO", usuario_id=user["id"], detalle={"aprobado_id": uid, "usuario": row["usuario"]}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/solicitudes", status_code=302)


@app.post("/admin/usuario/{uid}/rechazar")
def admin_rechazar(request: Request, uid: int, user: dict = Depends(require_admin)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row:
            conn.execute("UPDATE usuarios SET estado = 'RECHAZADO' WHERE id = ?", (uid,))
            registrar_auditoria(conn, "USUARIO_RECHAZADO", usuario_id=user["id"], detalle={"rechazado_id": uid, "usuario": row["usuario"]}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/solicitudes", status_code=302)


@app.post("/admin/usuario/{uid}/bloquear")
def admin_bloquear(request: Request, uid: int, user: dict = Depends(require_admin)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row and row["usuario"] != "admin":
            conn.execute("UPDATE usuarios SET estado = 'BLOQUEADO' WHERE id = ?", (uid,))
            registrar_auditoria(conn, "USUARIO_BLOQUEADO", usuario_id=user["id"], detalle={"bloqueado_id": uid}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/usuarios", status_code=302)


@app.post("/admin/usuario/{uid}/activar")
def admin_activar(request: Request, uid: int, user: dict = Depends(require_admin)):
    with get_db() as conn:
        row = _usuario_row(conn, uid)
        if row:
            if row["estado"] == "BLOQUEADO" and not puede_registrar_rol(conn, row["rol"]):
                return RedirectResponse("/admin/usuarios?error=cupo", status_code=302)
            conn.execute("UPDATE usuarios SET estado = 'ACTIVO' WHERE id = ?", (uid,))
            registrar_auditoria(conn, "USUARIO_ACTIVADO", usuario_id=user["id"], detalle={"activado_id": uid}, ip=client_ip(request), equipo=ua(request), resultado="OK")
    return RedirectResponse("/admin/usuarios", status_code=302)


@app.post("/admin/usuario/{uid}/rol")
async def admin_cambiar_rol(request: Request, uid: int, user: dict = Depends(require_admin)):
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



@app.post("/admin/usuario/{uid}/password_temp")
def admin_password_temp(request: Request, uid: int, user: dict = Depends(require_admin)):
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
def admin_eliminar_usuario(request: Request, uid: int, user: dict = Depends(require_admin)):
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
def admin_config_carpeta_pdf(request: Request, user: dict = Depends(require_admin), pdf_dir: str = Form(...)):
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
def admin_reindexar_legacy(request: Request, user: dict = Depends(require_admin)):
    return RedirectResponse("/admin/reindexar", status_code=302)


@app.post("/admin/reindexar/iniciar")
def admin_reindexar_iniciar(request: Request, user: dict = Depends(require_admin), modo: str = Query("rapida")):
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
    destino = Path(carpeta) / nombre
    if destino.exists():
        raise HTTPException(status_code=409,
            detail=f"Ya existe un memorando con ese número y fecha. Verificá el correlativo.")
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
