# Reporte del host en el servidor del laboratorio: wrapper, comando forzado (forced command) y disparador de n8n

El job se ejecuta en el host dueño del socket de Docker (`192.0.2.10`). n8n
solo lo programa y lo invoca: un nodo `Schedule` cada seis horas y un nodo `SSH`
que pide el comando forzado. Ninguna lógica de reporte vive en n8n.

## Por qué un comando forzado y no un shell

n8n guarda la clave privada de esta integración en su propio almacén de
credenciales. Una clave que abre un shell en el host es, por tanto, una clave a
todo lo que ese usuario puede alcanzar, y ese usuario necesita el socket de
Docker. La entrada de `authorized_keys` de abajo elimina el shell por completo:
`restrict` desactiva PTY, agente, puerto y reenvío X11, y `command=` reemplaza lo
que el cliente haya pedido ejecutar. El peor caso para una clave robada es
*iniciar un reporte*, que es además lo único que tiene que pasar.

## Qué es cada pieza

| Ruta | Rol |
| --- | --- |
| `/opt/host-report/current` | Enlace simbólico al directorio de release que ejecuta el wrapper |
| `/usr/local/bin/host-report-run` | `deploy/host-report/host-report-run`, el comando forzado |
| `/etc/host-report/env` | Entorno de la ejecución: gateway, hub, socket, ventana |
| `/etc/host-report/gateway.key` | Clave de aplicación para el gateway, `0600` |
| `/etc/host-report/hub.token` | Token de envío para el hub, `0600` |

El wrapper no acepta argumentos del cliente. Todo lo que varía —la ventana, el
destinatario, la clave de deduplicación— sale de `/etc/host-report/env` o del
reloj, así que un cliente no puede elegir a dónde va un reporte.

## La clave de deduplicación sin que el programador nos la diga

`host-report-run` deriva la clave del bloque de seis horas del día UTC:
`host-report:<host>:<YYYY-MM-DD>T<00|06|12|18>`. El hub reclama esa clave antes
de llamar a WAHA, así que una ejecución reintentada del mismo bloque responde
`duplicate` y no envía dos veces, mientras que el bloque siguiente es otro
reporte y debe enviarse. `HOST_REPORT_NOTIFY_KEY` en `/etc/host-report/env` anula
la clave derivada, para el día en que la cadencia deje de ser de seis horas.

## Instalación

Un solo comando como `root`. `install.sh` es idempotente: cada ejecución añade un
release nuevo, reapunta `current` y nunca sobrescribe un archivo de entorno, una
clave o un token que ya existan.

El reporte se ejecuta con una cuenta que ya existe y que ya alcanza el socket de
Docker: el instalador no crea ni usuarios ni permisos. Esa cuenta es por defecto
`$SUDO_USER`, así que la invocación más simple lo ejecuta con la cuenta en uso;
se pasa `--account NAME` cuando el reporte deba ejecutarse con otra. Su
grupo es dueño del directorio de configuración y de los secretos, porque el
comando forzado los lee con esa cuenta.

Se prepara la copia que el instalador copia. Solo se necesitan `tasks/host_report/`,
`host-report-run`, `vps-report-run`, `env.example` y `vps.env.example`, y ambos
fragmentos se ejecutan desde `deploy/host-report`:

```sh
cd deploy/host-report
staged=$(mktemp -d)
install -d "$staged/tasks"
cp -R ../../tasks/host_report "$staged/tasks/"
cp host-report-run vps-report-run env.example vps.env.example "$staged/"
```

Un directorio preparado al que le falte cualquiera de esos cinco se rechaza antes
de la primera escritura, así que un directorio a medio copiar nunca puede dejar
atrás un host a medio instalar.

Luego se instala, todavía en `deploy/host-report`, opcionalmente después de una
ejecución en seco. `--print-plan` funciona sin privilegios, imprime una línea
`would: ` por cada escritura prevista en orden de ejecución y no cambia nada:

```sh
sh install.sh --print-plan <staged-dir>
sudo sh install.sh <staged-dir>
# or, when the report runs as another account:
sudo sh install.sh --account hostreport <staged-dir>
```

Se crea `/opt/host-report/releases/<UTC timestamp>/` con `tasks/` (directorios
0755, archivos 0644), se apunta `/opt/host-report/current` a ese release mediante
un `ln -sfn` atómico, se instalan ambos wrappers como
`/usr/local/bin/host-report-run` y `/usr/local/bin/vps-report-run`
(`root:root 0755`), se crea `/etc/host-report` (`root:<report group> 0750`),
se instala `env.example` como `/etc/host-report/env` y `vps.env.example` como
`/etc/host-report/vps.env` (`root:<report group> 0640`) y se crean vacíos
`gateway.key` y `hub.token` (`root:<report group> 0640`). Ambos wrappers y ambos
archivos de entorno provienen de una sola ejecución a propósito: el VPS se
recolecta desde este mismo servidor, así que un segundo paso de instalación solo
significaría una segunda forma de quedar a medio instalar. El grupo es el de la
cuenta del reporte, así que el comando forzado puede leerlos; `0600 root:root` lo
dejaría fuera. Un archivo de entorno, una clave o un token que ya existan se dejan
intactos y el instalador lo indica.

Lo que queda es solo lo que necesita un secreto:

1. La clave de aplicación del gateway se coloca en `/etc/host-report/gateway.key`.
2. El token de envío del hub se coloca en `/etc/host-report/hub.token`.
3. Se añade la clave pública de la credencial de n8n —la mitad privada nunca sale
   de n8n— como una línea en el `authorized_keys` de la cuenta, y la clave pública
   de la credencial `vps-report` como otra línea distinta:

   ```text
   restrict,command="/usr/local/bin/host-report-run" ssh-ed25519 AAAA... n8n@lab
   restrict,command="/usr/local/bin/vps-report-run" ssh-ed25519 AAAA... n8n@vps-report
   ```

   El instalador imprime ambas líneas otra vez al final de una ejecución real,
   porque editar `authorized_keys` es una decisión sobre el acceso y sigue siendo
   manual. Las dos claves son pares distintos: revocar el reporte del VPS deja
   funcionando el reporte del servidor, y la clave de respaldo no es ninguna de
   las dos. Ese archivo se mantiene como propiedad de la cuenta con modo `0600`,
   y luego se comprueba que `host-report-run` y `vps-report-run` se ejecutan con
   esa cuenta.
4. Se rellenan `HOST_REPORT_SSH_TARGET` y, una vez conocidos los servicios,
   `HOST_REPORT_ENDPOINTS` en `/etc/host-report/vps.env`. Todo lo demás en ese
   archivo ya es correcto para este servidor.

## Acceso a Docker: la decisión abierta

El job lee logs de contenedores, así que la cuenta que lo ejecuta alcanza la API
de Docker. Ejecutar el job con la cuenta que ya alcanza el socket de Docker es el
valor por defecto simple aceptado; el proxy de solo lectura de abajo es
endurecimiento posterior, no un requisito previo. Pertenecer al grupo `docker`
equivale a `root` en el host, lo que haría que el comando forzado fuera casi
decorativo. La alternativa es un proxy de socket de solo lectura delante del
socket:

```yaml
services:
  docker-logs-proxy:
    image: tecnativa/docker-socket-proxy
    restart: unless-stopped
    environment:
      CONTAINERS: 1
      LOGS: 1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - 127.0.0.1:2375:2375
```

Luego `/etc/host-report/env` fija `HOST_REPORT_DOCKER_URL=http://127.0.0.1:2375`
y la cuenta del reporte nunca se une al grupo `docker`. El proxy expone solo
listado, inspección y logs de contenedores: sin `EXEC`, sin `POST`, sin `IMAGES`.
Esa es la decisión de transporte que sigue figurando como T5; hasta que se
despliegue, la cuenta de servicio necesita el grupo `docker` y la cuenta es tan
poderosa como `root`.

## El workflow de n8n

El workflow `vps-report` necesita **su propia credencial**, la que contiene la
clave `n8n@vps-report`. Reutilizar la credencial `host-report` parece funcionar y
luego ejecuta el reporte equivocado en silencio: sshd aplica la **primera**
entrada de `authorized_keys` que coincide con una clave, así que la línea antigua
de `host-report-run` gana y la línea de `vps-report-run` nunca se ejecuta. Dos
datos más que costaron una hora:

- La credencial apunta al **servidor recolector** (`192.0.2.10`, usuario
  `server`, puerto 22), nunca al VPS. El servidor es el cliente; el VPS es el
  sujeto.
- El botón **Test** de n8n siempre falla en una clave con comando forzado, porque
  pide su propio comando y sshd lo reemplaza con el reporte. Se guarda sin probar
  y se ejecuta el workflow en su lugar.

`deploy/n8n/host-report.json` contiene el workflow completo: un nodo `Schedule`
con intervalo de seis horas y un nodo `SSH` que ejecuta `host-report-run`.
Se importa y se elige la credencial `host-report` en el nodo SSH — el archivo
referencia esa credencial por nombre y mantiene un id de marcador de posición, así
que nada secreto viaja en el repositorio. El workflow se importa inactivo a
propósito: se habilita cuando una ejecución manual devuelva un código de salida
`0`.

## El reporte del VPS

El reporte del VPS reutiliza el job `host-report` existente: el host sobre el que
informa se selecciona mediante `/etc/host-report/vps.env`, así que no existe un
segundo pipeline. El VPS solo ejecuta los contenedores; no tiene puerto de
escucha entrante, ni job programado, ni credencial de gateway o hub. El servidor
recolector ejecuta `vps-report-run`, el comando forzado de una clave dedicada que
no es la clave de respaldo: revocar el reporte no toca los respaldos.

| Ruta | Rol |
| --- | --- |
| `/usr/local/bin/vps-report-run` | `deploy/host-report/vps-report-run`, el comando forzado |
| `/etc/host-report/vps.env` | Entorno de la ejecución del VPS, sembrado desde `vps.env.example` |

`vps.env.example` es el esqueleto de ese segundo archivo: la etiqueta del host, el
transporte SSH y su tiempo de espera, una lista de endpoints vacía con un ejemplo
de dos endpoints en un comentario, y las mismas rutas de gateway y hub que usa el
reporte del servidor, porque es el mismo reportero. No lleva ningún valor
secreto, solo las rutas de los dos archivos que ya los contienen.

`vps-report-run` está modelado sobre `host-report-run` y lee las mismas
anulaciones `HOST_REPORT_WINDOW`, `HOST_REPORT_SLOT_HOURS`, `HOST_REPORT_ROOT` y
`HOST_REPORT_ENV_FILE`, así que los dos wrappers no pueden divergir. También
ignora `SSH_ORIGINAL_COMMAND`: el programador no elige nada.

La diferencia es la clave de deduplicación, porque este wrapper se ejecuta en el
servidor y no en el host reportado: `vps-report:<host>:<YYYY-MM-DD>T<00|06|12|18>`,
donde `<host>` es `HOST_REPORT_HOST` (por defecto `vps`), nunca `hostname`. Un
reintento del mismo bloque responde `duplicate` y no envía nada; un
`HOST_REPORT_NOTIFY_KEY` explícito en `/etc/host-report/vps.env` gana sobre la
clave derivada.

Se instala el wrapper junto a su hermano y se restringe la clave pública dedicada
a él, en el `authorized_keys` de la cuenta:

```text
restrict,command="/usr/local/bin/vps-report-run" ssh-ed25519 AAAA... n8n@vps-report
```

`deploy/n8n/vps-report.json` contiene la programación: un nodo `Schedule` de seis
horas y un nodo `SSH` que ejecuta `vps-report-run`. Se importa, se elige la
credencial `vps-report` en el nodo SSH y se habilita cuando una ejecución manual
devuelva un código de salida `0`.

## Verificación

```sh
# On the host, as the report account, exactly what n8n will trigger:
sudo -u <report account> /usr/local/bin/host-report-run >/tmp/report.md

# A second run of the same slot must not send again:
sudo -u <report account> /usr/local/bin/host-report-run >/dev/null
# stderr says: host_report: hub result=duplicate
```

Desde n8n, se ejecuta el workflow una vez y se confirma que la ejecución tuvo
éxito con el reporte en stdout y `hub result=sent`; se lo ejecuta otra vez dentro
del mismo bloque de seis horas y se confirma `result=duplicate`.

El reporte del VPS sigue el mismo procedimiento con sus propios nombres:

```sh
# On the server, as the report account, exactly what n8n will trigger:
sudo -u <report account> /usr/local/bin/vps-report-run >/tmp/vps-report.md
# stderr says: host_report: hub result=sent, and the key names the VPS host
sudo -u <report account> /usr/local/bin/vps-report-run >/dev/null
# stderr says: host_report: hub result=duplicate
```
