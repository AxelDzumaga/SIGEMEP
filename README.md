# SIGEMEP

Sistema de Consulta y Auditoría de Memorandos. Backend en Python con FastAPI,
base de datos SQLite (FTS5 para búsqueda de texto completo), plantillas Jinja2
y extracción/indexado de PDF con PyMuPDF.

## Requisitos

- Python 3.11 o superior
- pip
- Una carpeta accesible con los PDF de memorandos (y, opcionalmente, otra para
  los archivos reservados)

## Variables de entorno

Ver `.env.example` para la lista completa. Las más importantes:

| Variable                  | Obligatoria | Descripción                                                              |
|---------------------------|-------------|---------------------------------------------------------------------------|
| `SIGEMEP_SESSION_SECRET`  | En producción | Secreto para firmar la cookie de sesión. Generar con `python3 -c "import secrets; print(secrets.token_hex(32))"`. Si no se define, la app genera y persiste uno en `.session_secret` (solo apto para desarrollo). |
| `SIGEMEP_PDF_DIR`         | No          | Carpeta de PDF de memorandos. Si no se define, usa un valor por defecto distinto según el sistema operativo (ver `config.py`). |
| `SIGEMEP_RESERVADOS_DIR`  | No          | Carpeta de archivos reservados. Mismo criterio que la anterior.          |
| `SIGEMEP_COOKIE_SECURE`   | No          | Poner en `1` solo si hay HTTPS real delante (proxy/balanceador). Agrega el flag `Secure` a la cookie de sesión. |

## Instalación en Windows (desarrollo)

1. Abrir PowerShell en la carpeta del proyecto.
2. Crear entorno virtual e instalar dependencias:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. (Opcional) Definir variables de entorno para esta sesión de PowerShell:
   ```
   $env:SIGEMEP_SESSION_SECRET = "una-cadena-larga-y-aleatoria"
   $env:SIGEMEP_PDF_DIR = "C:\ruta\a\BASE MEMORANDOS"
   ```
4. Ejecutar el servidor de desarrollo (con autorecarga):
   ```
   ejecutar_dev.bat
   ```
   o manualmente:
   ```
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```
5. Abrir `http://localhost:8000` (o `http://IP_DEL_SERVIDOR:8000` desde otra PC de la red).

Más detalle en `EJECUCION_WINDOWS.txt`.

## Instalación en Ubuntu (VM Proxmox / producción)

1. Instalar dependencias del sistema:
   ```
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip
   ```
2. Copiar el proyecto al servidor, por ejemplo en `/opt/sigemep`.
3. Crear el archivo de variables de entorno:
   ```
   cd /opt/sigemep
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_hex(32))"   # pegar el resultado en SIGEMEP_SESSION_SECRET dentro de .env
   nano .env
   ```
4. Para correr en modo desarrollo (con autorecarga, puerto 8001):
   ```
   chmod +x ejecutar_ubuntu.sh
   ./ejecutar_ubuntu.sh
   ```
   Este script crea el entorno virtual la primera vez si no existe, instala
   `requirements.txt`, carga `.env` y levanta `uvicorn --reload`.

5. Para producción, instalar como servicio systemd:
   ```
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt

   sudo useradd --system --home /opt/sigemep --shell /usr/sbin/nologin sigemep
   sudo chown -R sigemep:sigemep /opt/sigemep

   sudo cp sigemep.service /etc/systemd/system/sigemep.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now sigemep
   sudo systemctl status sigemep
   journalctl -u sigemep -f
   ```
   Revisar y ajustar en `sigemep.service` las rutas (`WorkingDirectory`,
   `EnvironmentFile`, `ExecStart`) si el proyecto no está en `/opt/sigemep`.

6. Abrir `http://IP_DEL_SERVIDOR:8000`.

### Por qué no usar varios workers de uvicorn

La indexación de PDF reporta su progreso en un diccionario en memoria del
proceso (no en la base de datos). Correr más de un proceso/worker rompería
ese seguimiento de progreso y podría causar contención de escritura en
SQLite. Por eso tanto `ejecutar_ubuntu.sh` como `sigemep.service` levantan un
solo proceso, igual que la configuración actual en Windows.

## Primer ingreso

- Usuario: `admin`
- Contraseña: `admin123`
- Se exige cambiar la contraseña en el primer ingreso.

Tras el primer arranque, entrar como admin, cambiar la contraseña y usar
«Actualizar base PDF» en el panel para indexar los PDF de la carpeta
configurada.

## Seguridad

- **Bloqueo de cuenta**: tras 5 intentos de login fallidos consecutivos, la
  cuenta pasa a estado `BLOQUEADO` y debe ser reactivada manualmente por un
  administrador desde *Usuarios*.
- **Expiración de sesión**: la sesión se cierra automáticamente tras 30
  minutos sin actividad (ver `SESSION_INACTIVITY_TIMEOUT_MINUTOS` en
  `config.py`).
- **Cabeceras HTTP**: todas las respuestas incluyen `X-Frame-Options`,
  `X-Content-Type-Options`, `Referrer-Policy` y una `Content-Security-Policy`
  básica.
- **Secreto de sesión**: nunca se hardcodea; se toma de
  `SIGEMEP_SESSION_SECRET`. Sin esa variable, se genera uno aleatorio en
  `.session_secret` (excluido de git) solo para no romper el flujo de
  desarrollo; en producción se recomienda definir la variable explícitamente.
- **Protección CSRF**: todo POST (formularios y llamadas fetch/XHR desde JS)
  exige un token guardado en la sesión del usuario. El token viaja como campo
  oculto `csrf_token` en los formularios normales, o como cabecera
  `X-CSRF-Token` en las llamadas JS que no parten de un `<form>`. Una request
  sin token válido recibe `403`.
- **Cookie de sesión `Secure`**: configurable con `SIGEMEP_COOKIE_SECURE=1`
  cuando el servidor esté detrás de HTTPS (ver `.env.example`). Desactivada
  por defecto para no romper despliegues en HTTP plano.
- **Límite de subida de PDF**: `/brigada/insertar_memorando/guardar` rechaza
  archivos por encima de `MAX_UPLOAD_MB` (50 MB por defecto, en `config.py`),
  validado del lado del servidor además del límite que ya existía en el JS.
- **SQLite en modo WAL**: permite lecturas concurrentes mientras hay una
  escritura en curso (por ejemplo, mientras corre una indexación en segundo
  plano), con `busy_timeout` para reintentar en vez de fallar al instante.
- Toda acción relevante (logins, descargas, bloqueos, cambios de usuarios,
  etc.) queda registrada en la tabla de auditoría, visible en el panel de
  administración.

## Estructura del proyecto

```
app.py                  # Rutas FastAPI
config.py                # Configuración central (rutas, secretos, límites)
services/
  auth_service.py         # Autenticación y validaciones de usuario
  audit_service.py         # Auditoría y registro de búsquedas
  db.py                    # Conexión SQLite, esquema y migraciones
  pdf_service.py            # Indexado, extracción y marca de agua de PDF
  search_service.py          # Búsqueda FTS5
  memo_creator.py              # Generación de memorandos nuevos en PDF
templates/                # Plantillas Jinja2
static/                    # CSS/JS
```

## Utilidades

- `check_users.py`: lista los usuarios registrados en la base de datos (útil
  para verificar estados tras un bloqueo o una migración).
