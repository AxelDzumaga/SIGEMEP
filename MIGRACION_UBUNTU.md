# Migración a Ubuntu (Proxmox) — Pasos faltantes

Este documento es la checklist de lo que falta hacer a mano cuando se migre
SIGEMEP de la PC madre (Windows) a la VM Ubuntu. El código ya está listo
(rutas multiplataforma, scripts de Ubuntu, systemd, etc. — ver `README.md`).
Lo que sigue es específico de **esta** migración: datos y secretos que git
no transporta.

## 1. Lo que se trae solo con git

```
git clone https://github.com/AxelDzumaga/SIGEMEP.git
```

Trae todo el código: `app.py`, `services/`, `templates/`, `static/`,
`config.py`, `requirements.txt`, `ejecutar_ubuntu.sh`, `sigemep.service`,
`.env.example`, `README.md`.

## 2. Lo que hay que copiar a mano desde la PC madre

Nada de esto está en git (a propósito: son datos y secretos, no código).

| Qué | Dónde está en la PC madre (Windows) | Dónde va en Ubuntu |
|---|---|---|
| **Base de datos** `database.db` | Carpeta del proyecto en producción | Raíz del proyecto clonado en Ubuntu |
| **Carpeta de PDF de memorandos** | La ruta configurada como `SIGEMEP_PDF_DIR` (por defecto `C:\Users\SIGEMEP\Desktop\REDCOMPARTIDA`, pero puede haber sido cambiada desde el panel «Indexar PDFs») | Cualquier carpeta en Ubuntu; la ruta exacta va en `SIGEMEP_PDF_DIR` del `.env` |
| **Carpeta de reservados** | La `SIGEMEP_RESERVADOS_DIR` actual (por defecto `...\INFORMES RV`) | Idem, en `SIGEMEP_RESERVADOS_DIR` |
| **`previews/primeras_hojas/`** | Carpeta del proyecto (opcional — son cachés de PNG, se regeneran solas al reindexar) | Opcional, se puede omitir y reindexar en Ubuntu |
| **`logs/`** | Carpeta del proyecto (opcional, solo historial) | Opcional |

No hace falta copiar `.session_secret` ni `.env`: en Ubuntu se genera/completa
uno nuevo (paso siguiente).

## 3. Pasos en la VM Ubuntu después de clonar

1. Crear el archivo de entorno:
   ```
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_hex(32))"   # pegar el resultado en SIGEMEP_SESSION_SECRET
   nano .env
   ```
   Completar también `SIGEMEP_PDF_DIR` y `SIGEMEP_RESERVADOS_DIR` con las
   rutas reales de Ubuntu donde se copiaron las carpetas.
2. Copiar `database.db` a la raíz del proyecto (si no se copia, arranca con
   una base vacía y el usuario `admin` / `admin123` por defecto).
3. Probar en modo desarrollo:
   ```
   chmod +x ejecutar_ubuntu.sh
   ./ejecutar_ubuntu.sh
   ```
   o instalar como servicio systemd (`sigemep.service`) para producción —
   instrucciones completas en `README.md`.
4. Entrar como admin y usar **«Actualizar base PDF»** / **«Indexar
   Reservados»** para reconstruir el índice de búsqueda contra las carpetas
   copiadas (esto regenera `previews/` automáticamente si no se copió).

## 4. Importante: la ruta de las carpetas también vive en la base de datos

`SIGEMEP_PDF_DIR` y `SIGEMEP_RESERVADOS_DIR` no solo se leen de `.env`: se
guardan también en la tabla `configuracion` de `database.db` la primera vez
que se inicializa, y el panel «Indexar PDFs» las puede sobrescribir después.

Si se copia la base de datos real desde la PC madre, **la ruta guardada ahí
va a seguir apuntando a la ruta de Windows** hasta que se entre a *Indexar
PDFs* / *Indexar Reservados* y se actualice manualmente a la ruta nueva de
Linux. Este es el primer paso a hacer después de copiar todo, antes de
reindexar.
