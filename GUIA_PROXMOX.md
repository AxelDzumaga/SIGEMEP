# Guía: crear la VM en Proxmox y levantar SIGEMEP desde cero

Esta guía cubre la parte que falta antes de `MIGRACION_UBUNTU.md`: crear
la VM en Proxmox, instalar Ubuntu Server desde la ISO, y dejarla lista
para clonar el repo. A partir de "clonar SIGEMEP" esto se enlaza con
`README.md` (instalación) y `MIGRACION_UBUNTU.md` (qué datos copiar de la
PC madre).

## 1. Descargar la ISO de Ubuntu Server

Descargá **Ubuntu Server LTS** (24.04 o la LTS vigente) desde la página
oficial de Ubuntu (`ubuntu.com/download/server`). Usá la versión "LTS",
no la de escritorio — no necesitás interfaz gráfica para esto.

## 2. Subir la ISO a Proxmox

1. En la interfaz web de Proxmox, elegí el storage donde vas a guardar
   ISOs (normalmente `local`).
2. `local` → pestaña **ISO Images** → **Upload**.
3. Seleccioná el archivo `.iso` descargado y subilo. Tarda según el
   tamaño (~2-3 GB) y la conexión.

## 3. Crear la VM

Botón **Create VM** (arriba a la derecha) y completar el wizard:

- **General**: Node (el host físico), VM ID (lo que sugiere Proxmox está
  bien), Name (ej. `sigemep-ubuntu`).
- **OS**: "Use CD/DVD disc image (iso)" → seleccionar la ISO subida en el
  paso anterior. Guest OS Type: `Linux`, Version: la que corresponda
  (`6.x - 2.6 Kernel` o similar, Proxmox la detecta solo).
- **System**: dejar los valores por defecto (BIOS `OVMF (UEFI)` o
  `SeaBIOS`, cualquiera funciona; si elegís UEFI, agregá también un disco
  EFI cuando lo pida). Activar **Qemu Agent** si querés ver la IP desde
  Proxmox después.
- **Disks**: 32 GB como piso razonable. Sumále el espacio que ocupen los
  PDF de memorandos/reservados si pensás guardarlos dentro de la VM en vez
  de un storage de red — esa carpeta puede crecer bastante.
- **CPU**: 2 cores alcanza para esta carga (FastAPI + SQLite + indexación
  esporádica de PDFs, no es una app pesada).
- **Memory**: 2048-4096 MB. PyMuPDF e indexar muchos PDFs de golpe puede
  picos de memoria; con 4 GB sobra.
- **Network**: Bridge `vmbr0` (o el que tengas mapeado a la red donde van
  a estar los usuarios que acceden a SIGEMEP).
- **Confirm**: revisar y **Finish**.

## 4. Instalar Ubuntu Server

1. Seleccionar la VM → **Start**, después **Console**.
2. En el instalador de Ubuntu: idioma, teclado, tipo de instalación
   ("Ubuntu Server", no "minimized" para tener más herramientas a mano).
3. **Network**: con DHCP alcanza para empezar; si la VM va a ser el
   servidor permanente, convení con quien administra la red una IP fija
   (DHCP reservation, o configurarla estática acá mismo).
4. **Storage**: "Use entire disk" sobre el disco virtual creado en el
   paso 3 es lo más simple.
5. **Profile setup**: creá el usuario que vas a usar para administrar
   (ej. `sigemep-admin`). Anotá la contraseña.
6. **SSH**: activar "Install OpenSSH server" — vas a querer conectarte por
   SSH en vez de usar la consola de Proxmox para todo lo que sigue.
7. **Featured snaps**: no es necesario ninguno para esto, podés saltear.
8. Esperar a que termine la instalación → **Reboot Now**.
9. Cuando reinicie, quitar la ISO virtual: VM → **Hardware** → CD/DVD
   Drive → editar → "Do not use any media" (si no, puede volver a bootear
   el instalador).

## 5. Primer arranque

Conectate por SSH (`ssh sigemep-admin@<ip-de-la-vm>`) y dejá el sistema
listo:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
```

Si Qemu Agent quedó activado en el paso 3, la IP de la VM aparece
directamente en el resumen de la VM en Proxmox; si no, `ip a` desde la
consola te la muestra.

## 6. Clonar SIGEMEP y seguir con la instalación normal

```bash
git clone https://github.com/AxelDzumaga/SIGEMEP.git
cd SIGEMEP
```

De acá en adelante son los pasos que ya están documentados:

1. **Qué copiar a mano desde la PC madre** (base de datos, carpetas de
   PDF/reservados) → `MIGRACION_UBUNTU.md`.
2. **Configurar `.env` e instalar** → sección "Instalación en Ubuntu" de
   `README.md`.
3. Probar con `./ejecutar_ubuntu.sh` (modo dev, puerto 8001) antes de
   instalarlo como servicio.
4. Instalar `sigemep.service` con systemd para que quede corriendo
   siempre, incluso después de reiniciar la VM (`README.md` tiene los
   comandos exactos: `useradd`, copiar el `.service`, `systemctl enable
   --now`).

## 7. Acceso desde la red

Por defecto Ubuntu Server no trae firewall activo, así que el puerto 8000
(o 8001 en modo dev) ya queda accesible desde la red. Si activás `ufw`,
abrí el puerto que estés usando:

```bash
sudo ufw allow 8000/tcp
```

Para entrar desde cualquier PC de la red: `http://IP_DE_LA_VM:8000`.

## 8. Problemas comunes

- **No carga la página desde otra PC**: revisá que el bridge de red de la
  VM (`vmbr0` u otro) esté en la misma red/VLAN que las PCs que necesitan
  acceder, y que no haya un firewall (`ufw` o el del router) bloqueando el
  puerto.
- **`ejecutar_ubuntu.sh: Permission denied`**: falta `chmod +x
  ejecutar_ubuntu.sh` (los permisos de ejecución no siempre sobreviven una
  copia manual de archivos, sí sobreviven un `git clone`).
- **El servicio systemd no arranca**: `journalctl -u sigemep -f` muestra
  el error real. Lo más común es una ruta mal puesta en `WorkingDirectory`
  / `EnvironmentFile` / `ExecStart` del archivo `sigemep.service`, o que
  falte crear el venv antes de habilitar el servicio.
- **La carpeta de PDFs configurada sigue siendo la de Windows**: ver la
  nota al final de `MIGRACION_UBUNTU.md` — hay que actualizarla desde el
  panel "Indexar PDFs" después de copiar la base de datos real.
