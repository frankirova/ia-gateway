# Reporte del VPS: el lado de solo lectura

El VPS es un *sujeto*, nunca un participante. Tiene un ejecutable, una cuenta sin
shell y una entrada de clave; nunca tiene una clave de gateway, un token de hub ni
una programación. El reporte en sí se ejecuta en el servidor que ya informa sobre
sí mismo.

## El contrato

`vps-readonly` imprime exactamente un documento JSON en stdout y nada más:

```json
{
  "version": 1,
  "collected_at": "2026-11-13T00:00:00Z",
  "truncated": false,
  "host": {
    "cpu_count": 2,
    "load": [1.02, 0.8, 0.5],
    "uptime_seconds": 90061,
    "mem_total_kb": 4000000,
    "mem_available_kb": 1200000,
    "swap_total_kb": 0,
    "swap_free_kb": 0,
    "filesystems": [
      {"mount": "/", "total_bytes": 1, "used_bytes": 1, "avail_bytes": 1, "used_percent": 42}
    ]
  },
  "containers": [
    {"listing": {"Id": "…", "Names": ["web"], "Image": "nginx:1"},
     "inspect": {"Name": "/web", "RestartCount": 0, "State": {"Status": "running"}},
     "logs": "2026-11-13T00:00:00.000000000Z started"}
  ]
}
```

Ignora todo argumento y toda variable de entorno: el cliente no elige ni la
ventana, ni el destinatario, ni la clave. `SSH_ORIGINAL_COMMAND` nunca se lee.
Cuando no se puede alcanzar docker imprime `{"version": 1, "error":
"docker_unavailable"}` y sale con código de salida `1`.

Por contenedor emite la entrada de *listado* reformada a la forma de la API de
Docker (`Id`, `Names`, `Image`), el documento de inspección completo y el texto de
logs con marcas de tiempo, desde `docker ps`, `docker inspect` y `docker logs`
invocados como listas de argv — nunca a través de un shell. Un contenedor cuya
llamada de inspección o de logs falla se conserva en el paquete con un campo
`error`; la primera falla gana, así que un problema posterior nunca enmascara el
anterior. Ningún contenedor se descarta en silencio.

## Instalación en el VPS

Requiere `python3` (solo biblioteca estándar) y un CLI de docker que la cuenta del
reporte pueda ejecutar.

```sh
# 1. one identity, no password login, docker access for read-only CLI use
#    The shell cannot be /usr/sbin/nologin: sshd runs a forced command through
#    the login shell (`shell -c command`), so nologin would print its message
#    and exit instead of running vps-readonly. The restriction comes from
#    `restrict,command=` on the key, not from the shell.
sudo useradd --system --no-create-home --shell /bin/sh vps-report
sudo passwd -d vps-report
sudo usermod -aG docker vps-report

# 2. the program, root-owned and not writable by the report account
sudo install -o root -g root -m 0755 vps-readonly /usr/local/bin/vps-readonly

# 3. the dedicated key, restricted to exactly this command
sudo install -d -o vps-report -g vps-report -m 0700 /home/vps-report/.ssh
# The home directory must belong to the account or sshd refuses under
# StrictModes, and `install -d` creates the parents as root.
sudo chown vps-report:vps-report /home/vps-report
# paste the server's public key, then:
sudo vi /home/vps-report/.ssh/authorized_keys
# restrict,command="/usr/local/bin/vps-readonly" ssh-ed25519 AAAA… report@server
```

`restrict` elimina pty, el reenvío y toda otra capacidad; `command=` fija el
programa remoto. Una clave de cliente robada vale un paquete de solo lectura.

Dos trampas contra las que ya ha chocado esta instalación, ambas invisibles hasta
que el cliente intenta conectarse:

- **La cuenta debe figurar en `AllowUsers`** cuando el VPS restringe SSH a
  usuarios con nombre. Sin eso el servidor responde
  `Permission denied (publickey)` incluso con una clave perfecta, y `fail2ban`
  acaba contestando `Connection refused`.
- **El shell debe ser un shell real.** Con `nologin`, el `shell -c command` de
  sshd imprime "This account is currently not available." y nunca ejecuta el
  programa.

## Qué expone el VPS

Por defecto viaja todo contenedor en ejecución. Un archivo propiedad de root
reduce ese conjunto, y es el único lugar donde vive la lista de permitidos:

```sh
sudo tee /etc/vps-readonly.json >/dev/null <<'JSON'
{"containers": ["app-api", "app-worker", "api", "postgres"]}
JSON
sudo chown root:root /etc/vps-readonly.json
sudo chmod 0644 /etc/vps-readonly.json
```

- **Ausente** el archivo: todo contenedor en ejecución, exactamente el
comportamiento anterior.
- **Presente**: solo se inspeccionan los contenedores nombrados y se leen sus
logs, así que el documento se mantiene pequeño y nunca se envía un contenedor de
fuera.
- **Lista vacía**: solo el estado del host. Deliberado, nunca accidental.
- **Inutilizable** (no es JSON, clave desconocida, nombre que no es una cadena):
el programa responde `{"version": 1, "error": "config_invalid"}` y sale con código
de salida `1`. Una errata no debe reportar en silencio un conjunto distinto del
que el operador pidió.

Un contenedor configurado que no está en ejecución se reporta en el paquete bajo
`selection.missing`, y el reporte convierte cada uno en un hallazgo medido
(`⛔ Servicio sin correr: <name>`). Ese es justo el objetivo de nombrarlos: un
servicio caído es noticia, un log callado no.

## Verificar

```sh
# from the server, once, so the host key is pinned before BatchMode is used
ssh vps-report@<vps> true

# the bundle, exactly as the report receives it
ssh vps-report@<vps> vps-readonly | python3 -m json.tool | head -40

# a wrong command must be refused by the forced command, not executed
ssh vps-report@<vps> id
```

El último comando sigue imprimiendo un paquete: `command=` ignora lo que pide el
cliente. Eso es lo esperado, no un fallo.

Cuando `/etc/vps-readonly.json` existe, el documento lleva un objeto `selection`
con los nombres configurados y los que no están en ejecución:

```sh
ssh vps-report@<vps> vps-readonly | python3 -c \
  'import json,sys; print(json.load(sys.stdin).get("selection"))'
```

## Límites conocidos

- **Cobertura de logs**: `--tail 20000` por contenedor desde el momento de la
  llamada, así que una ventana mayor de lo que cubren esas líneas queda
  subrepresentada. El paquete no tiene parámetro de ventana a propósito.
- **Presupuesto por contenedor**: cada contenedor conserva sus líneas completas
  más recientes hasta 300 KB y activa `logs_truncated`. Medido en el VPS real: sin
  esto, un postgres llevaba 4.2 MB y un mock llevaba 1.8 MB, y el techo del
  documento vaciaba 11 de los 15 contenedores — el reporte mostraría entonces
  silencio donde quiere decir "no se pudo transportar".
- **Desfase de reloj** en el borde de la ventana: el reporte descarta las líneas
  más antiguas que su propia ventana después de analizar la marca de tiempo de la
  línea, así que un reloj del VPS desviado unos minutos desplaza el límite otros
  minutos. Las líneas sin marca de tiempo analizable se conservan.
- **Tamaño**: el documento está limitado a 8 MiB. Cuando aun así se alcanza ese
  techo, los logs más grandes se descartan uno a uno y se activa `truncated`,
  nunca los datos del host.
- **El grupo docker es root con otro nombre.** La cuenta sigue sin shell y su
  clave sigue forzada a `vps-readonly`, pero cualquiera que alcance la ejecución
  de código de `vps-readonly` en el VPS podría alcanzar el socket. El programa se
  mantiene como propiedad de root.
