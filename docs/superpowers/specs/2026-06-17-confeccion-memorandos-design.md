# Módulo de Confección de Memorandos — Spec de Diseño

**Fecha:** 2026-06-17  
**Estado:** Aprobado por usuario, pendiente de implementación

---

## Resumen

Agregar a SIGEMEP_APP_DEV un módulo que permita a usuarios con rol **BRIGADA** redactar un memorando desde un formulario web, ver una vista previa del PDF generado y guardarlo directamente en la carpeta de indexación configurada. El ADMIN lo indexa luego con el botón "Actualizar base PDF" ya existente.

---

## Contexto

El flujo actual requiere:
1. Redactar el memorando en Word con un template existente
2. Exportar a PDF manualmente
3. Abrir el programa de escritorio SIGEMEP 3.0 para renombrar el PDF
4. Copiar el archivo a la carpeta de indexación

Este módulo integra los pasos 2, 3 y 4 dentro de la app web.

---

## Alcance

### Incluido
- Formulario web con todos los campos del memorando
- Generación de PDF en memoria con PyMuPDF (fitz)
- Vista previa como imagen PNG antes de guardar
- Nombre de archivo auto-generado según convención existente
- Guardado del PDF en la carpeta de PDFs configurada
- Registro en auditoría
- Botón "Nuevo memorando" en el dashboard de brigada

### Excluido
- Indexación automática (queda a cargo del ADMIN)
- Subida de archivos Word existentes
- Edición de memorandos ya guardados
- Numeración automática del correlativo

---

## Roles

| Rol | Puede crear memorando | Puede indexar |
|---|---|---|
| BRIGADA | Sí | No |
| JEFE | No | No |
| ADMIN | No | Sí (flujo existente) |

---

## Estructura del memorando

Basada en los PDFs reales indexados en el sistema:

```
                    M E M O R A N D O
                    920-12-000.{NRO}/{AÑO}.-
                                                    {iniciales}

BUENOS AIRES, {DD} de {MES} de {AÑO}.-
DE: {DE}
A: {A}

ASUNTO:    "COMUNICAR NOVEDAD"

HECHO: "{HECHO}"

{TIPO_FECHA}: {DD/MM/YYYY}            HORA: {hora}

LUGAR DEL HECHO: {lugar}

{ETIQUETA_PERSONA}: {persona}

IMPUTADO/S: {imputado}

ELEMENTOS SUSTRAÍDOS: {elementos_sustraidos}

ELEMENTOS SECUESTRADOS: {elementos_secuestrados}

DEPENDENCIA PREVENTORA: {dependencia}

MAGISTRADO INTERVENTOR: {magistrado}

BREVE RESEÑA:

{resena}
```

---

## Campos del formulario

| Campo | Tipo | Valor por defecto | Requerido |
|---|---|---|---|
| Número correlativo | Entero | — | Sí |
| Año | Entero | Año actual | Sí |
| Iniciales del autor | Texto corto | — | No |
| Fecha del memorando | Fecha | Hoy | Sí |
| DE | Texto | "Departamento CONTROL DE INTEGRIDAD PROFESIONAL.-" | Sí |
| A | Texto | — | Sí |
| HECHO | Texto (mayúsculas) | — | Sí |
| Tipo de fecha del hecho | Selector | "FECHA DEL HECHO" | Sí |
| Fecha del hecho | Fecha | — | Sí |
| Hora | Texto | — | No |
| Lugar del hecho | Texto largo | — | Sí |
| Etiqueta persona | Selector (`DAMNIFICADO` / `DAMNIFICADA` / `DENUNCIANTE` / `PARTES`) | "DAMNIFICADO" | Sí |
| Datos de persona | Texto largo | — | No |
| Imputado/s | Texto largo | — | No |
| Elementos sustraídos | Texto | "No hubo." | Sí |
| Elementos secuestrados | Texto | "No hubo." | Sí |
| Dependencia preventora | Texto largo | — | No |
| Magistrado interventor | Texto largo | — | No |
| Breve reseña | Área de texto | — | Sí |

---

## Convención de nombre de archivo

```
{NRO}-"{HECHO_LIMPIO}"-{YYYY-MM-DD}.pdf
```

- **NRO**: el correlativo ingresado (ej: `145`)
- **HECHO_LIMPIO**: HECHO en mayúsculas, sin caracteres inválidos para sistema de archivos (`\ / : * ? < > |` eliminados, `"` mantenidas como parte del formato). Los espacios se conservan tal cual.
- **YYYY-MM-DD**: fecha del hecho formateada

Ejemplos:
- `145-"ROBO AGRAVADO"-2026-01-03.pdf`
- `1-"LESIONES CULPOSAS"-2026-06-17.pdf`

La lógica es equivalente a `proponer_nombre_pdf()` + `_limpiar()` de `SIGEMEP 3.0/modules/renamer.py`.

---

## Arquitectura

### Archivo nuevo: `services/memo_creator.py`

Dos funciones públicas:

```python
def nombre_archivo_memorando(campos: dict) -> str:
    """Genera el nombre del archivo según la convención."""

def generar_pdf_memorando(campos: dict) -> bytes:
    """Genera el PDF en memoria y devuelve los bytes."""
```

Responsabilidades de `generar_pdf_memorando`:
- Crear un documento fitz en modo escritura
- Aplicar márgenes (2.5 cm izquierda/derecha, 2.5 cm arriba/abajo)
- Renderizar encabezado centrado: "M E M O R A N D O", número, iniciales
- Renderizar cada sección con etiqueta en negrita y valor
- Líneas separadoras donde corresponde
- Devolver `bytes` del PDF sin escribir a disco

### Rutas nuevas en `app.py`

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/brigada/nuevo_memorando` | Muestra el formulario |
| POST | `/brigada/nuevo_memorando/preview` | Genera PNG de preview (AJAX, devuelve JSON `{png_b64, nombre}`) |
| POST | `/brigada/nuevo_memorando/guardar` | Guarda el PDF en disco y redirige |

Las tres protegidas con `require_roles("BRIGADA")`.

### Template nuevo: `templates/nuevo_memorando.html`

- Hereda de `base.html`
- Layout de dos columnas:
  - Izquierda: formulario con todos los campos
  - Derecha: panel de vista previa (PNG cargado por AJAX)
- Nombre de archivo generado visible en tiempo real (actualizado con JS al cambiar NRO, HECHO o fecha del hecho)
- Botón **"Vista previa"**: llama al endpoint preview por AJAX, muestra el PNG
- Botón **"Guardar memorando"**: deshabilitado hasta que se genere al menos una vista previa

### Cambio en `templates/dashboard_brigada.html`

Agregar botón "Nuevo memorando" que navega a `/brigada/nuevo_memorando`.

---

## Flujo de datos

```
Usuario completa formulario
        │
        ▼
[Clic "Vista previa"]
POST /brigada/nuevo_memorando/preview
        │
        ├─ memo_creator.generar_pdf_memorando(campos) → bytes PDF
        ├─ fitz renderiza página 1 como PNG
        └─ JSON {png_b64, nombre_archivo} → mostrado en panel derecho
        │
        ▼
[Clic "Guardar memorando"]
POST /brigada/nuevo_memorando/guardar
        │
        ├─ memo_creator.generar_pdf_memorando(campos) → bytes PDF
        ├─ memo_creator.nombre_archivo_memorando(campos) → nombre.pdf
        ├─ carpeta_pdf_actual() → ruta destino
        ├─ Verificar que no existe el archivo (si existe → error)
        ├─ Escribir PDF en disco
        ├─ registrar_auditoria("MEMORANDO_CREADO", detalle={nombre, carpeta})
        └─ Redirect /dashboard/brigada con mensaje de éxito
```

---

## Manejo de errores

| Situación | Comportamiento |
|---|---|
| Nombre de archivo ya existe en la carpeta | Error claro: "Ya existe un memorando con ese número y fecha. Verificá el correlativo." |
| Carpeta de PDFs no accesible | Error: "La carpeta de destino no está disponible. Contactá al administrador." |
| Campos requeridos vacíos | Validación en frontend (HTML required) y backend (HTTPException 422) |
| Error al generar el PDF | Error genérico con log en servidor |

---

## Auditoría

Nueva acción registrada: `MEMORANDO_CREADO`

```python
registrar_auditoria(
    conn,
    "MEMORANDO_CREADO",
    usuario_id=user["id"],
    detalle={"nombre_archivo": nombre, "carpeta": str(carpeta)},
    ip=client_ip(request),
    equipo=ua(request),
    resultado="OK",
)
```

No requiere cambios en `audit_service.py`.

---

## Dependencias

Sin dependencias nuevas. Usa exclusivamente:
- `PyMuPDF` (fitz) — ya en `requirements.txt`
- `fastapi`, `jinja2` — ya presentes
