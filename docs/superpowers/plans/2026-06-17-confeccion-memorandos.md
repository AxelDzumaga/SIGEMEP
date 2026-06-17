# Módulo de Confección de Memorandos — Plan de Implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permitir a usuarios BRIGADA redactar memorandos desde un formulario web, ver una vista previa del PDF generado y guardarlo en la carpeta de indexación para que el ADMIN lo indexe después.

**Architecture:** Nuevo servicio `memo_creator.py` genera el PDF en memoria con PyMuPDF (fitz). Tres rutas nuevas en `app.py` manejan el formulario, la preview AJAX y el guardado. El template `nuevo_memorando.html` presenta formulario + panel de vista previa en dos columnas.

**Tech Stack:** FastAPI, PyMuPDF (fitz) — ambos ya instalados. Sin dependencias nuevas.

---

## Mapa de archivos

| Archivo | Acción | Responsabilidad |
|---|---|---|
| `services/memo_creator.py` | **Crear** | Naming del archivo y generación del PDF |
| `app.py` | **Modificar** | 3 rutas nuevas al final del archivo |
| `templates/nuevo_memorando.html` | **Crear** | Formulario con preview AJAX |
| `templates/dashboard_brigada.html` | **Modificar** | Botón "Nuevo memorando" en línea 74 |

---

## Task 1: `services/memo_creator.py` — función de naming

**Files:**
- Create: `services/memo_creator.py`

- [ ] **Step 1: Crear el archivo con la función `nombre_archivo_memorando`**

Crear `C:\SIGEMEP_APP_DEV\services\memo_creator.py` con este contenido:

```python
"""Generación de PDFs de memorandos institucionales."""
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import fitz

_MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}

_CHARS_INVALIDOS = re.compile(r'[\\/:*?<>|]')


def _limpiar(texto: str) -> str:
    texto = texto.replace("\n", " ").replace("\r", " ")
    texto = re.sub(r"\s+", " ", texto)
    texto = _CHARS_INVALIDOS.sub("", texto)
    return texto.strip().strip(".- ").strip()


def nombre_archivo_memorando(campos: dict) -> str:
    """Genera el nombre del archivo: {NRO}-\"{HECHO}\"-{YYYY-MM-DD}.pdf"""
    nro = str(campos.get("nro", "")).strip() or "SIN-NRO"
    hecho = _limpiar((campos.get("hecho", "") or "SIN RESENA").upper())
    fecha_str = campos.get("fecha_hecho", "") or ""
    try:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        fecha = "SIN-FECHA"
    return f'{nro}-"{hecho}"-{fecha}.pdf'
```

- [ ] **Step 2: Verificar la función de naming**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.memo_creator import nombre_archivo_memorando
casos = [
    {'nro': '145', 'hecho': 'Robo Agravado', 'fecha_hecho': '2026-06-17'},
    {'nro': '1',   'hecho': 'LESIONES CULPOSAS', 'fecha_hecho': '2026-01-01'},
    {'nro': '10',  'hecho': 'amenazas',    'fecha_hecho': '2026-03-15'},
]
for c in casos:
    print(nombre_archivo_memorando(c))
"
```

Salida esperada:
```
145-"ROBO AGRAVADO"-2026-06-17.pdf
1-"LESIONES CULPOSAS"-2026-01-01.pdf
10-"AMENAZAS"-2026-03-15.pdf
```

---

## Task 2: `services/memo_creator.py` — generación del PDF

**Files:**
- Modify: `services/memo_creator.py`

- [ ] **Step 1: Agregar constantes y helpers de layout al final del archivo**

Añadir después de `nombre_archivo_memorando`:

```python
# ── Layout (puntos; 1 cm ≈ 28.35 pt; A4 = 595 × 842 pt) ──────────
_PW  = 595
_PH  = 842
_ML  = 72    # margen izquierdo
_MR  = 523   # margen derecho
_MT  = 72    # margen superior
_FS_TITULO  = 14
_FS_NORMAL  = 11
_FS_SMALL   = 9
_LH         = 16


def _fuente_sistema() -> str | None:
    for ruta in [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/verdana.ttf",
    ]:
        if Path(ruta).exists():
            return ruta
    return None


_FONT_PATH = _fuente_sistema()


def _tw(page: fitz.Page, texto: str, fontname: str, fs: float) -> float:
    return page.get_textlength(texto, fontname=fontname, fontsize=fs)


def _texto(page: fitz.Page, y: float, texto: str, fontname: str,
           fs: float, alinear: str = "izq", x: float | None = None) -> float:
    """Inserta una línea y retorna el y siguiente."""
    if alinear == "centro":
        x = (_PW - _tw(page, texto, fontname, fs)) / 2
    elif alinear == "der":
        x = _MR - _tw(page, texto, fontname, fs)
    else:
        x = x if x is not None else _ML
    page.insert_text((x, y), texto, fontname=fontname, fontsize=fs, color=(0, 0, 0))
    return y + fs * 1.45


def _bloque(page: fitz.Page, y: float, texto: str, fontname: str,
            fs: float, altura_max: float = 150) -> float:
    """Inserta texto con wrap y retorna el y siguiente."""
    rect = fitz.Rect(_ML, y, _MR, y + altura_max)
    sobrante = page.insert_textbox(
        rect, texto, fontname=fontname, fontsize=fs, color=(0, 0, 0), align=0
    )
    if sobrante < 0:
        return y + altura_max + 3
    lineas = max(1, texto.count("\n") + 1)
    return y + lineas * fs * 1.45 + 3
```

- [ ] **Step 2: Agregar `generar_pdf_memorando` al final del archivo**

```python
def generar_pdf_memorando(campos: dict) -> bytes:
    """Genera el PDF del memorando en memoria y devuelve los bytes."""
    doc  = fitz.open()
    page = doc.new_page(width=_PW, height=_PH)

    # Registrar fuente del sistema para soporte de caracteres latinos
    fn = fn_b = "helv"
    if _FONT_PATH:
        page.insert_font(fontname="F0", fontfile=_FONT_PATH)
        fn = fn_b = "F0"

    y = _MT

    # ── ENCABEZADO ────────────────────────────────────────────────
    y = _texto(page, y, "M E M O R A N D O", fn_b, _FS_TITULO, "centro")
    y += 4
    nro_pad  = str(campos.get("nro", "")).strip().zfill(3)
    anio     = str(campos.get("anio", date.today().year)).strip()
    y = _texto(page, y, f"920-12-000.{nro_pad}/{anio}.-", fn, _FS_NORMAL, "der")
    iniciales = (campos.get("iniciales") or "").strip()
    if iniciales:
        y = _texto(page, y, iniciales, fn, _FS_SMALL, "der")
    y += _LH

    # Fecha encabezado
    try:
        d    = datetime.strptime(campos.get("fecha_memo", ""), "%Y-%m-%d")
        mes  = _MESES_ES[d.month].capitalize()
        fecha_enc = f"BUENOS AIRES, {d.day:02d} de {mes} de {d.year}.-"
    except (ValueError, KeyError):
        fecha_enc = "BUENOS AIRES, __ de ________ de ____.-"
    y = _texto(page, y, fecha_enc, fn, _FS_NORMAL, "der")
    y += _LH

    # ── DE / A / ASUNTO ──────────────────────────────────────────
    de_val = (campos.get("de") or "Departamento CONTROL DE INTEGRIDAD PROFESIONAL.-").strip()
    y = _bloque(page, y, f"DE: {de_val}", fn, _FS_NORMAL)
    a_val  = (campos.get("a") or "").strip()
    y = _bloque(page, y, f"A: {a_val}", fn, _FS_NORMAL)
    y += _LH / 2
    y = _texto(page, y, 'ASUNTO:    "COMUNICAR NOVEDAD"', fn, _FS_NORMAL)
    y += _LH

    # ── HECHO ────────────────────────────────────────────────────
    hecho = (campos.get("hecho") or "").strip().upper()
    y = _texto(page, y, f'HECHO: "{hecho}"', fn_b, _FS_NORMAL)
    y += _LH / 2

    # ── FECHA Y HORA ─────────────────────────────────────────────
    tipo_fecha  = (campos.get("tipo_fecha") or "FECHA DEL HECHO").strip()
    fecha_hecho = campos.get("fecha_hecho", "")
    try:
        dh       = datetime.strptime(fecha_hecho, "%Y-%m-%d")
        fecha_fmt = f"{dh.day:02d}/{dh.month:02d}/{dh.year}"
    except ValueError:
        fecha_fmt = fecha_hecho
    hora = (campos.get("hora") or "").strip()
    linea_fecha = f"{tipo_fecha}: {fecha_fmt}."
    if hora:
        linea_fecha += f"            HORA: {hora}"
    y = _texto(page, y, linea_fecha, fn, _FS_NORMAL)
    y += _LH / 2

    # ── LUGAR ────────────────────────────────────────────────────
    lugar = (campos.get("lugar") or "").strip()
    if lugar:
        y = _bloque(page, y, f"LUGAR DEL HECHO: {lugar}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── PERSONA ──────────────────────────────────────────────────
    etiqueta = (campos.get("etiqueta_persona") or "DAMNIFICADO").strip()
    persona  = (campos.get("persona") or "").strip()
    if persona:
        y = _bloque(page, y, f"{etiqueta}: {persona}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── IMPUTADO ─────────────────────────────────────────────────
    imputado = (campos.get("imputado") or "").strip()
    if imputado:
        y = _bloque(page, y, f"IMPUTADO/S: {imputado}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── ELEMENTOS ────────────────────────────────────────────────
    sustraidos   = (campos.get("elementos_sustraidos")   or "No hubo.").strip()
    secuestrados = (campos.get("elementos_secuestrados") or "No hubo.").strip()
    y = _texto(page, y, f"ELEMENTOS SUSTRAIDOS: {sustraidos}", fn, _FS_NORMAL)
    y += _LH / 2
    y = _texto(page, y, f"ELEMENTOS SECUESTRADOS: {secuestrados}", fn, _FS_NORMAL)
    y += _LH / 2

    # ── DEPENDENCIA ──────────────────────────────────────────────
    dependencia = (campos.get("dependencia") or "").strip()
    if dependencia:
        y = _bloque(page, y, f"DEPENDENCIA PREVENTORA: {dependencia}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── MAGISTRADO ───────────────────────────────────────────────
    magistrado = (campos.get("magistrado") or "").strip()
    if magistrado:
        y = _bloque(page, y, f"MAGISTRADO INTERVENTOR: {magistrado}", fn, _FS_NORMAL)
        y += _LH / 2

    # ── LÍNEA SEPARADORA ─────────────────────────────────────────
    page.draw_line((_ML, y), (_MR, y), color=(0, 0, 0), width=0.5)
    y += _LH

    # ── BREVE RESEÑA ─────────────────────────────────────────────
    y = _texto(page, y, "BREVE RESENA:", fn_b, _FS_NORMAL)
    y += _LH / 2
    resena = (campos.get("resena") or "").strip()
    if resena:
        _bloque(page, y, resena, fn, _FS_NORMAL, altura_max=_PH - y - _MT)

    buf = doc.tobytes()
    doc.close()
    return buf
```

- [ ] **Step 3: Generar un PDF de prueba y verificarlo visualmente**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, 'C:/SIGEMEP_APP_DEV')
from services.memo_creator import generar_pdf_memorando
campos = {
    'nro': '1', 'anio': '2026', 'iniciales': 'f.m.ch.',
    'fecha_memo': '2026-06-17',
    'de': 'Departamento CONTROL DE INTEGRIDAD PROFESIONAL.-',
    'a': 'ARCHIVO 920.-',
    'hecho': 'ROBO AGRAVADO',
    'tipo_fecha': 'FECHA DEL HECHO', 'fecha_hecho': '2026-06-17',
    'hora': '03:30 aproximadamente.',
    'lugar': 'Interseccion Av. Corrientes y Callao, CABA.',
    'etiqueta_persona': 'DAMNIFICADO',
    'persona': 'Agente LP 12345 (DNI 30.000.001) Juan PEREZ, Comisaria 1ra.',
    'imputado': 'N.N. masculino profugo.',
    'elementos_sustraidos': 'No hubo.',
    'elementos_secuestrados': 'No hubo.',
    'dependencia': 'Comisaria 3ra., Policia de la Prov. de Buenos Aires.',
    'magistrado': 'UFI N°2, a cargo de la Dra. Maria GARCIA.',
    'resena': 'En el dia de la fecha, siendo las 04:00 horas, se tomo conocimiento mediante escucha de la frecuencia policial del hecho descripto.',
}
pdf = generar_pdf_memorando(campos)
open('C:/SIGEMEP_APP_DEV/test_memo.pdf', 'wb').write(pdf)
print(f'PDF generado: {len(pdf)} bytes -> test_memo.pdf')
"
```

Abrir `C:\SIGEMEP_APP_DEV\test_memo.pdf` para verificar el formato visualmente.

- [ ] **Step 4: Eliminar el PDF de prueba**

```powershell
Remove-Item C:\SIGEMEP_APP_DEV\test_memo.pdf -Force
```

---

## Task 3: Rutas en `app.py`

**Files:**
- Modify: `app.py` (agregar al final, antes de la ruta `/acceso_denegado`)

- [ ] **Step 1: Agregar el import de memo_creator**

En `app.py`, buscar el bloque de imports de services (líneas 21-39) y agregar:

```python
from services.memo_creator import generar_pdf_memorando, nombre_archivo_memorando
```

- [ ] **Step 2: Agregar las 3 rutas nuevas**

Agregar justo antes de la línea `@app.get("/acceso_denegado"...`:

```python
# ── CONFECCIÓN DE MEMORANDOS (BRIGADA) ────────────────────────────

@app.get("/brigada/nuevo_memorando", response_class=HTMLResponse)
def nuevo_memorando_get(
    request: Request,
    user: Annotated[dict[str, Any], Depends(require_roles("BRIGADA"))],
):
    from datetime import date as _date
    return templates.TemplateResponse("nuevo_memorando.html", {
        "request": request,
        "user": user,
        "anio_actual": _date.today().year,
        "fecha_hoy": _date.today().isoformat(),
        "error": None,
        "campos": None,
    })


@app.post("/brigada/nuevo_memorando/preview")
async def nuevo_memorando_preview(
    request: Request,
    user: Annotated[dict[str, Any], Depends(require_roles("BRIGADA"))],
):
    import base64
    form = await request.form()
    campos = dict(form)
    pdf_bytes = generar_pdf_memorando(campos)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    png_bytes = doc[0].get_pixmap(dpi=120).tobytes("png")
    doc.close()
    return JSONResponse({
        "png_b64": base64.b64encode(png_bytes).decode(),
        "nombre":  nombre_archivo_memorando(campos),
    })


@app.post("/brigada/nuevo_memorando/guardar")
async def nuevo_memorando_guardar(
    request: Request,
    user: Annotated[dict[str, Any], Depends(require_roles("BRIGADA"))],
):
    from datetime import date as _date
    form   = await request.form()
    campos = dict(form)
    nombre = nombre_archivo_memorando(campos)
    with get_db() as conn:
        carpeta = carpeta_pdf_actual(conn)
    destino = Path(carpeta) / nombre
    if destino.exists():
        return templates.TemplateResponse("nuevo_memorando.html", {
            "request":     request,
            "user":        user,
            "error":       f"Ya existe un memorando con ese número y fecha: {nombre}. Verificá el correlativo.",
            "campos":      campos,
            "anio_actual": campos.get("anio", _date.today().year),
            "fecha_hoy":   campos.get("fecha_memo", _date.today().isoformat()),
        }, status_code=409)
    try:
        destino.write_bytes(generar_pdf_memorando(campos))
    except Exception as exc:
        logger.error("Error generando memorando: %s", exc)
        return templates.TemplateResponse("nuevo_memorando.html", {
            "request":     request,
            "user":        user,
            "error":       "Error al generar el PDF. Contactá al administrador.",
            "campos":      campos,
            "anio_actual": campos.get("anio", _date.today().year),
            "fecha_hoy":   campos.get("fecha_memo", _date.today().isoformat()),
        }, status_code=500)
    with get_db() as conn:
        registrar_auditoria(
            conn, "MEMORANDO_CREADO",
            usuario_id=user["id"],
            detalle={"nombre_archivo": nombre, "carpeta": str(carpeta)},
            ip=client_ip(request),
            equipo=ua(request),
            resultado="OK",
        )
    return RedirectResponse(f"/dashboard/brigada?memo_creado={nombre}", status_code=302)
```

- [ ] **Step 3: Verificar que `fitz` esté importado en `app.py`**

`fitz` no está importado actualmente en `app.py` (lo usa `pdf_service.py`). Agregar al bloque de imports estándar al comienzo del archivo:

```python
import fitz
```

- [ ] **Step 4: Verificar que la app arranca sin errores**

```powershell
C:\SIGEMEP_APP_DEV\venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'C:/SIGEMEP_APP_DEV')
import app
print('OK — imports correctos')
"
```

Esperado: `OK — imports correctos`

---

## Task 4: Template `templates/nuevo_memorando.html`

**Files:**
- Create: `templates/nuevo_memorando.html`

- [ ] **Step 1: Crear el template**

Crear `C:\SIGEMEP_APP_DEV\templates\nuevo_memorando.html`:

```html
{% extends "base.html" %}
{% block title %}Nuevo Memorando — SIGEMEP{% endblock %}
{% block content %}
<div class="nm-wrap">

  <div class="nm-head">
    <h2 class="nm-titulo">Confeccionar Memorando</h2>
    {% if error %}<div class="nm-error">{{ error }}</div>{% endif %}
    <div class="nm-nombre-vivo">
      <span class="nm-nombre-lbl">Archivo:</span>
      <span id="nm-nombre">—</span>
    </div>
  </div>

  <div class="nm-body">

    <!-- ── FORMULARIO ──────────────────────────────────────── -->
    <form id="nm-form" class="nm-form" method="post" action="/brigada/nuevo_memorando/guardar">

      <section class="nm-sec">
        <h4 class="nm-sec-titulo">Identificación</h4>
        <div class="nm-row">
          <label>N° Memorando <span class="nm-req">*</span>
            <input type="number" name="nro" id="nm-nro" min="1" required
                   value="{{ campos.nro if campos else '' }}" placeholder="Ej: 145">
          </label>
          <label>Año <span class="nm-req">*</span>
            <input type="number" name="anio" min="2000" max="2099" required
                   value="{{ campos.anio if campos else anio_actual }}">
          </label>
          <label>Iniciales autor
            <input type="text" name="iniciales" maxlength="20"
                   value="{{ campos.iniciales if campos else '' }}" placeholder="Ej: f.m.ch.">
          </label>
        </div>
        <label>Fecha del memorando <span class="nm-req">*</span>
          <input type="date" name="fecha_memo" required
                 value="{{ campos.fecha_memo if campos else fecha_hoy }}">
        </label>
      </section>

      <section class="nm-sec">
        <h4 class="nm-sec-titulo">Destinatario</h4>
        <label>DE <span class="nm-req">*</span>
          <input type="text" name="de" required
                 value="{{ campos.de if campos else 'Departamento CONTROL DE INTEGRIDAD PROFESIONAL.-' }}">
        </label>
        <label>A <span class="nm-req">*</span>
          <input type="text" name="a" required
                 value="{{ campos.a if campos else '' }}" placeholder="Ej: ARCHIVO 920.-">
        </label>
      </section>

      <section class="nm-sec">
        <h4 class="nm-sec-titulo">Hecho</h4>
        <label>Carátula (HECHO) <span class="nm-req">*</span>
          <input type="text" name="hecho" id="nm-hecho" required
                 value="{{ campos.hecho if campos else '' }}"
                 placeholder="Ej: ROBO AGRAVADO" style="text-transform:uppercase">
        </label>
        <div class="nm-row">
          <label>Tipo de fecha <span class="nm-req">*</span>
            <select name="tipo_fecha">
              <option value="FECHA DEL HECHO"   {% if not campos or campos.get('tipo_fecha','')=='FECHA DEL HECHO'   %}selected{% endif %}>FECHA DEL HECHO</option>
              <option value="FECHA DE DENUNCIA" {% if campos and campos.get('tipo_fecha')==' FECHA DE DENUNCIA' %}selected{% endif %}>FECHA DE DENUNCIA</option>
              <option value="FECHA"             {% if campos and campos.get('tipo_fecha')=='FECHA'              %}selected{% endif %}>FECHA</option>
            </select>
          </label>
          <label>Fecha <span class="nm-req">*</span>
            <input type="date" name="fecha_hecho" id="nm-fecha-hecho" required
                   value="{{ campos.fecha_hecho if campos else '' }}">
          </label>
          <label>Hora
            <input type="text" name="hora"
                   value="{{ campos.hora if campos else '' }}" placeholder="Ej: 03:30 aproximadamente.">
          </label>
        </div>
        <label>Lugar del hecho <span class="nm-req">*</span>
          <textarea name="lugar" rows="2" required placeholder="Dirección completa del hecho">{{ campos.lugar if campos else '' }}</textarea>
        </label>
      </section>

      <section class="nm-sec">
        <h4 class="nm-sec-titulo">Personas involucradas</h4>
        <label>Tipo de persona
          <select name="etiqueta_persona">
            <option value="DAMNIFICADO"  {% if not campos or campos.get('etiqueta_persona','')=='DAMNIFICADO'  %}selected{% endif %}>DAMNIFICADO</option>
            <option value="DAMNIFICADA"  {% if campos and campos.get('etiqueta_persona')=='DAMNIFICADA'  %}selected{% endif %}>DAMNIFICADA</option>
            <option value="DENUNCIANTE"  {% if campos and campos.get('etiqueta_persona')=='DENUNCIANTE'  %}selected{% endif %}>DENUNCIANTE</option>
            <option value="PARTES"       {% if campos and campos.get('etiqueta_persona')=='PARTES'       %}selected{% endif %}>PARTES</option>
          </select>
        </label>
        <label>Datos
          <textarea name="persona" rows="3" placeholder="Nombre, LP, DNI, dependencia">{{ campos.persona if campos else '' }}</textarea>
        </label>
        <label>Imputado/s
          <textarea name="imputado" rows="2" placeholder="Nombre/s o N.N.">{{ campos.imputado if campos else '' }}</textarea>
        </label>
      </section>

      <section class="nm-sec">
        <h4 class="nm-sec-titulo">Elementos</h4>
        <label>Sustraídos
          <input type="text" name="elementos_sustraidos"
                 value="{{ campos.elementos_sustraidos if campos else 'No hubo.' }}">
        </label>
        <label>Secuestrados
          <input type="text" name="elementos_secuestrados"
                 value="{{ campos.elementos_secuestrados if campos else 'No hubo.' }}">
        </label>
      </section>

      <section class="nm-sec">
        <h4 class="nm-sec-titulo">Actuación judicial</h4>
        <label>Dependencia preventora
          <textarea name="dependencia" rows="2" placeholder="Comisaría / Dependencia">{{ campos.dependencia if campos else '' }}</textarea>
        </label>
        <label>Magistrado interventor
          <textarea name="magistrado" rows="2" placeholder="Nombre, cargo y dependencia">{{ campos.magistrado if campos else '' }}</textarea>
        </label>
      </section>

      <section class="nm-sec">
        <h4 class="nm-sec-titulo">Breve reseña</h4>
        <label><span class="nm-req">*</span>
          <textarea name="resena" rows="8" required placeholder="Narrativa del hecho...">{{ campos.resena if campos else '' }}</textarea>
        </label>
      </section>

      <div class="nm-acciones">
        <button type="button" id="nm-btn-preview" class="btn-secondary">Vista previa</button>
        <button type="submit" id="nm-btn-guardar" class="btn-primary" disabled>Guardar memorando</button>
      </div>

    </form>

    <!-- ── PANEL PREVIEW ───────────────────────────────────── -->
    <aside class="nm-preview">
      <div class="nm-sec-titulo" style="margin-bottom:10px">Vista previa</div>
      <p class="nm-preview-placeholder" id="nm-ph">Completá los campos y hacé clic en "Vista previa"</p>
      <img id="nm-img" src="" alt="Vista previa" style="display:none;max-width:100%;border-radius:4px">
      <div id="nm-preview-err" class="nm-error" style="display:none"></div>
    </aside>

  </div><!-- /nm-body -->
</div><!-- /nm-wrap -->

<style>
:root { --naranja: #e87820; }
.nm-wrap  { max-width:1400px; margin:0 auto; padding:16px; }
.nm-head  { margin-bottom:16px; }
.nm-titulo { color:var(--naranja); font-size:1.4rem; margin:0 0 8px; }
.nm-error { background:#3a1010; color:#ff6b6b; border:1px solid #c0392b; border-radius:6px; padding:10px 14px; margin:8px 0; font-size:.9rem; }
.nm-nombre-vivo { font-size:.85rem; color:#888; margin-top:6px; }
.nm-nombre-lbl  { color:#555; margin-right:6px; }
#nm-nombre { color:var(--naranja); font-weight:700; }
.nm-body  { display:flex; gap:20px; align-items:flex-start; }
.nm-form  { flex:1; min-width:0; display:flex; flex-direction:column; gap:12px; }
.nm-sec   { background:#1a1a1a; border:1px solid #2e2e2e; border-radius:8px; padding:14px; display:flex; flex-direction:column; gap:10px; }
.nm-sec-titulo { color:var(--naranja); font-size:.75rem; font-weight:700; text-transform:uppercase; letter-spacing:1px; margin:0; }
.nm-row   { display:flex; gap:10px; flex-wrap:wrap; }
.nm-row > label { flex:1; min-width:110px; }
.nm-form label { display:flex; flex-direction:column; gap:4px; color:#bbb; font-size:.85rem; }
.nm-form input,
.nm-form select,
.nm-form textarea { background:#111; border:1px solid #3a3a3a; border-radius:5px; color:#eee; padding:7px 10px; font-size:.9rem; transition:border-color .15s; resize:vertical; }
.nm-form input:focus,
.nm-form select:focus,
.nm-form textarea:focus { outline:none; border-color:var(--naranja); }
.nm-req { color:var(--naranja); }
.nm-acciones { display:flex; gap:10px; justify-content:flex-end; padding-top:4px; }
.nm-preview { width:360px; flex-shrink:0; background:#1a1a1a; border:1px solid #2e2e2e; border-radius:8px; padding:14px; position:sticky; top:20px; }
.nm-preview-placeholder { color:#444; font-size:.85rem; text-align:center; padding:40px 0; margin:0; }
@media(max-width:900px){ .nm-body { flex-direction:column; } .nm-preview { width:100%; position:static; } }
</style>

<script>
(function(){
  const nroEl   = document.getElementById('nm-nro');
  const hechoEl = document.getElementById('nm-hecho');
  const fechaEl = document.getElementById('nm-fecha-hecho');
  const nombreEl = document.getElementById('nm-nombre');
  const btnPrev = document.getElementById('nm-btn-preview');
  const btnGuard= document.getElementById('nm-btn-guardar');
  const img     = document.getElementById('nm-img');
  const ph      = document.getElementById('nm-ph');
  const errEl   = document.getElementById('nm-preview-err');

  function limpiar(s){ return s.toUpperCase().replace(/[\\/:*?<>|]/g,''); }

  function actualizarNombre(){
    const n = (nroEl.value||'').trim();
    const h = limpiar((hechoEl.value||'').trim());
    const f = (fechaEl.value||'').trim();
    nombreEl.textContent = (n && h && f) ? n+'- "'+h+'"-'+f+'.pdf' : '—';
  }

  [nroEl, hechoEl, fechaEl].forEach(el => el && el.addEventListener('input', actualizarNombre));
  actualizarNombre();

  btnPrev.addEventListener('click', function(){
    btnPrev.disabled = true;
    btnPrev.textContent = 'Generando...';
    errEl.style.display = 'none';
    fetch('/brigada/nuevo_memorando/preview', {
      method: 'POST',
      body: new FormData(document.getElementById('nm-form'))
    })
    .then(r => { if(!r.ok) throw new Error(r.status); return r.json(); })
    .then(function(j){
      img.src = 'data:image/png;base64,'+j.png_b64;
      img.style.display = 'block';
      ph.style.display  = 'none';
      if(j.nombre) nombreEl.textContent = j.nombre;
      btnGuard.disabled = false;
    })
    .catch(function(){
      errEl.textContent = 'Error al generar la vista previa. Revisá los campos obligatorios.';
      errEl.style.display = 'block';
    })
    .finally(function(){
      btnPrev.disabled = false;
      btnPrev.textContent = 'Vista previa';
    });
  });
})();
</script>
{% endblock %}
```

---

## Task 5: Botón y mensaje de éxito en `dashboard_brigada.html`

**Files:**
- Modify: `templates/dashboard_brigada.html` (línea 74)
- Modify: `app.py` (ruta `/dashboard/brigada`)

- [ ] **Step 1: Modificar la ruta `/dashboard/brigada` en `app.py` para recibir el mensaje de éxito**

Buscar la función `dashboard_brigada` en `app.py` y agregar el parámetro `memo_creado`:

```python
@app.get("/dashboard/brigada", response_class=HTMLResponse)
def dashboard_brigada(
    request: Request,
    user: Annotated[dict[str, Any], Depends(require_roles("BRIGADA"))],
    memo_creado: Optional[str] = Query(None),
):
    return templates.TemplateResponse("dashboard_brigada.html", {
        "request": request,
        "user": user,
        "memo_creado": memo_creado,
    })
```

- [ ] **Step 2: Agregar el banner de éxito en `dashboard_brigada.html`**

Agregar justo después de `{% block content %}` (o del primer `<div>` del contenido):

```html
{% if memo_creado %}
<div class="brigada-memo-ok">
  Memorando guardado correctamente: <strong>{{ memo_creado }}</strong>
</div>
{% endif %}
```

Y agregar el estilo en el bloque `<style>` existente:

```css
.brigada-memo-ok {
  background: #0d2a1a;
  border: 1px solid #1a7a3a;
  color: #4caf87;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
  font-size: .9rem;
}
```

- [ ] **Step 3: Agregar el botón "Nuevo memorando"**

En `templates/dashboard_brigada.html`, encontrar el bloque (línea ~74):

```html
<div class="brigada-hero-actions">
    <a href="/buscar" class="brigada-btn brigada-btn-primary"><i class="fa-solid fa-magnifying-glass"></i> Buscar memorando</a>
    <a href="/cambiar_password" class="brigada-btn"><i class="fa-solid fa-key"></i> Cambiar contraseña</a>
</div>
```

Reemplazarlo con:

```html
<div class="brigada-hero-actions">
    <a href="/buscar" class="brigada-btn brigada-btn-primary"><i class="fa-solid fa-magnifying-glass"></i> Buscar memorando</a>
    <a href="/brigada/nuevo_memorando" class="brigada-btn"><i class="fa-solid fa-file-pen"></i> Nuevo memorando</a>
    <a href="/cambiar_password" class="brigada-btn"><i class="fa-solid fa-key"></i> Cambiar contraseña</a>
</div>
```

---

## Task 6: Verificación end-to-end

- [ ] **Step 1: Iniciar la app**

```powershell
Start-Process -NoNewWindow `
  -FilePath "C:\SIGEMEP_APP_DEV\venv\Scripts\uvicorn.exe" `
  -ArgumentList "app:app","--host","0.0.0.0","--port","8001" `
  -WorkingDirectory "C:\SIGEMEP_APP_DEV" `
  -RedirectStandardError "C:\SIGEMEP_APP_DEV\logs\dev_stderr.log"
Start-Sleep -Seconds 5
Get-Content "C:\SIGEMEP_APP_DEV\logs\dev_stderr.log"
```

Esperado: `Application startup complete.` sin errores.

- [ ] **Step 2: Verificar que la ruta requiere autenticación**

```powershell
$r = Invoke-WebRequest "http://localhost:8001/brigada/nuevo_memorando" -MaximumRedirection 0 -ErrorAction SilentlyContinue
"Status: $($r.StatusCode)"
```

Esperado: `Status: 302`

- [ ] **Step 3: Probar el flujo completo en el navegador**

1. Abrir `http://localhost:8001/login` e ingresar con un usuario BRIGADA
2. Verificar que el dashboard muestra el botón "Nuevo memorando"
3. Hacer clic → verificar que el formulario carga correctamente
4. Completar todos los campos requeridos
5. Hacer clic en "Vista previa" → verificar que aparece el PNG del PDF en el panel derecho
6. Verificar que el nombre de archivo se actualiza en tiempo real
7. Hacer clic en "Guardar memorando" → verificar redirección al dashboard
8. Verificar que el PDF aparece en la carpeta configurada con el nombre correcto

- [ ] **Step 4: Verificar auditoría**

Ingresar como ADMIN → `/admin/auditoria` → verificar registro `MEMORANDO_CREADO` con el nombre del archivo.

- [ ] **Step 5: Detener la app**

```powershell
Get-Process | Where-Object { $_.Name -like "*uvicorn*" } | Stop-Process -Force
```
