# Reporte de logs del host — collector, writer y delivery (T1, T2, T3, T6)

Recolección determinista para el job programado del reporte del host, más el
writer opcional que le pide al gateway que lo interprete. Una ejecución lista
todos los contenedores en ejecución del host, lee una ventana acotada de sus
logs, quita las secuencias de escape ANSI, los resume, enmascara los secretos
obvios e imprime un único digest. Cuando se alcanza el tope de líneas de un
contenedor, las líneas de error y de advertencia se retienen antes que las
informativas y los conteos quedan registrados.

El modo por defecto nunca llama a un modelo de lenguaje: imprime el digest. Con
`--report markdown` la ejecución renderiza un prompt acotado, hace como máximo
una llamada al gateway, valida la respuesta de forma estricta e imprime un
reporte markdown con los hechos deterministas y la interpretación del modelo en
secciones separadas. Con `--notify hub` además envía un resumen compacto a
WhatsApp a través del hub personal, que es el dueño de la sesión de WhatsApp y
del token de envío. El modelo nunca calcula conteos, severidades, reinicios ni
códigos de salida.

```
python -m tasks.host_report --since 24h --format json
python -m tasks.host_report --report markdown
python -m tasks.host_report --report markdown --notify hub
```

Códigos de salida:

| Código | Significado |
| --- | --- |
| `0` | Reporte escrito en stdout |
| `1` | Una fuente es inalcanzable: el endpoint de Docker (o su listado) en local, el bundle SSH en remoto. El reporte y el mensaje se producen igual primero |
| `2` | Argumentos o valores de entorno inválidos |
| `3` | El prompt renderizado supera el presupuesto de caracteres |
| `4` | `--strict-model` y no se pudo producir ninguna interpretación |
| `5` | `--notify hub` y la entrega falló; el reporte sigue en stdout |

Solo el reporte va a stdout. Los diagnósticos van a stderr. Un fallo en un
contenedor nunca hace fallar la ejecución: ese contenedor termina en `skipped`.

## Módulos

| Módulo | Responsabilidad |
| --- | --- |
| `cli.py` | Análisis de argumentos, códigos de salida, disciplina de stdout/stderr |
| `settings.py` | Resolución de CLI/entorno/valor por defecto y los topes |
| `window.py` | Duraciones de `--since` y la ventana en epoch que recibe Docker |
| `docker_api.py` | Cliente HTTP de Docker Engine (socket unix o TCP), framing de logs |
| `ansi.py` | Eliminación de secuencias de escape ANSI de las líneas de log ya decodificadas |
| `collector.py` | Resumen por contenedor, topes, omisiones, armado del digest |
| `clustering.py` | Normalización de líneas, severidad, clusters de mensajes |
| `timestamps.py` | Manejo del prefijo `timestamps=1` de Docker |
| `redaction.py` | Enmascarado de credenciales |
| `report.py` | Renderizado JSON y de texto |
| `prompt.py` | Render acotado del prompt, aplicación del presupuesto, descartes registrados |
| `gateway.py` | Una llamada al gateway, con verificación de TLS, sin reintentos |
| `interpretation.py` | Validación estricta de la respuesta del modelo |
| `markdown.py` | Reporte markdown: hechos, interpretación, metadatos |
| `writer.py` | Umbral de omisión, la llamada única, fallback solo con hechos |
| `delivery.py` | Mensaje compacto, clave de deduplicación, una llamada autenticada al hub |

## Transporte

La API HTTP de Docker Engine se usa solo en lectura aquí: `GET /containers/json?all=0`,
`GET /containers/{id}/json` y
`GET /containers/{id}/logs?stdout=1&stderr=1&timestamps=1&since=<epoch>&tail=<n>`.

Solo biblioteca estándar, sin SDK de Docker. Cada llamada tiene timeout y nunca
se reintenta. Los cuerpos de log que no son TTY son un stream multiplexado de
cabeceras de frame de 8 bytes (tipo de stream, tres bytes en cero, longitud del
payload en big-endian); un cuerpo que no empieza con una cabecera de frame
plausible se lee como texto plano.

## Opciones

| CLI | Entorno | Por defecto | Significado |
| --- | --- | --- | --- |
| `--docker-url URL` | `HOST_REPORT_DOCKER_URL` | `unix:///var/run/docker.sock` | `unix:///path/to.sock` o `http://host:port` |
| `--since DURATION` | `HOST_REPORT_SINCE` | `24h` | Ventana que termina ahora: `<amount><unit>` con `s`, `m`, `h` o `d` |
| `--format json\|text` | — | `json` | Formato de salida del digest, usado cuando `--report` es `none` |
| `--timeout SECONDS` | `HOST_REPORT_TIMEOUT` | `10` | Timeout por petición a Docker, mayor que cero |
| `--max-lines N` | `HOST_REPORT_MAX_LINES` | `2000` | Líneas de log analizadas por contenedor después del muestreo |
| `--max-clusters N` | `HOST_REPORT_MAX_CLUSTERS` | `25` | Clusters conservados por contenedor |
| `--max-containers N` | `HOST_REPORT_MAX_CONTAINERS` | `50` | Contenedores recolectados |
| — | `HOST_REPORT_OVERFETCH` | `4` | El `tail` que se pide a Docker es `max_lines * overfetch`, acotado a `20000` |
| — | `HOST_REPORT_CONTAINER_LIMITS` | none | Objeto JSON con topes por contenedor; ver más abajo |
| `--ignore REGEX` | `HOST_REPORT_IGNORE` | ver más abajo | Línea descartada antes del clustering; repetible, separada por comas en el entorno |
| `--report none\|markdown` | — | `none` | `markdown` renderiza el reporte y llama al gateway una vez |
| `--strict-model` | — | off | Con `--report markdown`, sale con `4` en lugar de un fallback solo con hechos |
| `--prompt-budget N` | `HOST_REPORT_PROMPT_BUDGET` | `3200` | Tope duro de caracteres del prompt renderizado |
| `--notify none\|hub` | `HOST_REPORT_NOTIFY` | `none` | `hub` envía el resumen compacto a WhatsApp; requiere `--report markdown` |
| — | `HOST_REPORT_HOST` | `socket.gethostname()` | Nombre del host que se imprime en el digest y en el mensaje |
| — | `HOST_REPORT_TARGET` | `local` | `local` lee el socket de Docker de esta máquina; `ssh` lee un host remoto a través de un único bundle |
| — | `HOST_REPORT_SSH_TARGET` | none | `user@host` para `HOST_REPORT_TARGET=ssh`; la clave ya debe estar en el `authorized_keys` de esa cuenta |
| — | `HOST_REPORT_SSH_TIMEOUT` | `60` | Segundos para leer el bundle remoto completo |
| — | `HOST_REPORT_SSH_BINARY` | `ssh` | Binario cliente; una costura para los tests, no una vía de escape |
| — | `HOST_REPORT_ENDPOINTS` | none | Lista JSON de `{"name", "url", "expect_status", "timeout_seconds"}` sondeada desde esta máquina, una vez cada uno |

Ajustes del gateway, que se leen solo cuando se usa `--report markdown`:

| Entorno | Por defecto | Significado |
| --- | --- | --- |
| `HOST_REPORT_GATEWAY_URL` | none | URL base; `POST <base>/v1/chat/completions` |
| `HOST_REPORT_GATEWAY_KEY_FILE` | none | Archivo que contiene la clave bearer de la aplicación; no existe ninguna bandera para la clave |
| `HOST_REPORT_GATEWAY_CA_BUNDLE` | none | Bundle de CA, obligatorio cuando la URL es `https` |
| `HOST_REPORT_GATEWAY_MODEL` | `e4b` | Alias público del modelo |
| `HOST_REPORT_MAX_TOKENS` | `400` | Presupuesto de tokens de salida |
| `HOST_REPORT_TIMEOUT_SECONDS` | `180` | Timeout de la petición |

Ajustes de entrega, que se leen solo cuando se usa `--notify hub`:

| Entorno | Por defecto | Significado |
| --- | --- | --- |
| `HOST_REPORT_HUB_URL` | none | URL base; `POST <base>/send` |
| `HOST_REPORT_HUB_TOKEN_FILE` | none | Archivo que contiene el token de envío del hub; no existe ninguna bandera para el token |
| `HOST_REPORT_HUB_CA_BUNDLE` | none | Bundle de CA, obligatorio cuando la URL es `https` |
| `HOST_REPORT_HUB_CHAT` | `owner` | Rol del destinatario: `owner` o `business` |
| `HOST_REPORT_HUB_TIMEOUT_SECONDS` | `10` | Timeout de la petición |
| `HOST_REPORT_NOTIFY_KEY` | la ventana | Clave de deduplicación explícita; el programador aporta su propio id de slot |

La precedencia es la bandera de CLI, después la variable de entorno y después el
valor por defecto. Las entradas de `--ignore` se suman a los valores por
defecto; los valores por defecto son `^\s*$` (líneas vacías), `(?i)healthcheck`
y `(?i)\b(?:GET|HEAD|POST)\s+/(?:health|healthz|live|ready|ping)\b`.

### Topes por contenedor

`HOST_REPORT_CONTAINER_LIMITS` sobrescribe los topes de contenedores con nombre
en lugar de agregar un segundo pipeline. Es un objeto JSON que mapea el nombre
de un contenedor a un objeto con las claves opcionales `max_lines` y
`max_clusters`:

```
HOST_REPORT_CONTAINER_LIMITS='{"dockge-dockge-1": {"max_lines": 5000, "max_clusters": 40}}'
```

- Una clave ausente conserva el valor global por defecto
  (`HOST_REPORT_MAX_LINES` o `HOST_REPORT_MAX_CLUSTERS`), así un contenedor
  puede sobrescribir solo un tope.
- El valor se valida al arrancar: un JSON malformado, un valor que no es un
  objeto JSON y las claves desconocidas, los valores no enteros o los valores
  menores que `1` dentro de una entrada son errores de configuración y salen con
  `2` antes de contactar a Docker.
- Un nombre que no está en la ejecución queda sin usar, no es un error: el mismo
  ajuste funciona en hosts que no corren todos los contenedores listados.

## Modo reporte

`--report markdown` reemplaza el digest de stdout por un reporte markdown.
Imprime el digest sin cambios cuando `--report` no está presente (`none`, el
valor por defecto), así los recolectores programados que ya usan la salida JSON
no se ven afectados.

1. **Render acotado.** Solo entran al prompt los contenedores con
   `error_lines > 0`, `restart_count >
   0`, un código de salida distinto de cero o al menos un cluster
   `error`/`warning`. Cada uno aporta su nombre, los reinicios, el código de
   salida, la cantidad de errores y como máximo tres
   clusters (primero el de mayor conteo); cada cluster aporta una línea de
   muestra concreta ya enmascarada — nunca la plantilla normalizada, así el
   modelo no tiene ningún `<num>` ni `<ts>` que pueda citar como si fuera dato —
   recortada a 120 caracteres y marcada con `…` cuando se recorta. Los
   contenedores se ordenan por cantidad de errores descendente y después por
   nombre.
2. **Presupuesto antes de la llamada.** El prompt renderizado (texto de sistema
   y de usuario) debe caber en `HOST_REPORT_PROMPT_BUDGET`. Mientras no quepa,
   se descarta primero el cluster de menor conteo y luego contenedores enteros
   (primero el de menor cantidad de errores). Cada descarte queda registrado en
   los metadatos del reporte. Si ni siquiera el bloque de evidencia vacío cabe,
   la ejecución sale con `3` sin llamar al modelo.
3. **Umbral de omisión.** Si ningún contenedor califica, no hay ninguna llamada
   al gateway: el reporte dice que no hay nada que reportar y sus metadatos
   quedan marcados como omitidos.
4. **Una sola llamada.** `POST <base>/v1/chat/completions` con `Authorization: Bearer
   <key from file>`, `model` tomado de `HOST_REPORT_GATEWAY_MODEL` (por defecto
   `e4b`), `temperature` `0`, `reasoning` `false`, `max_tokens` tomado de
   `HOST_REPORT_MAX_TOKENS` (por defecto `400`), `stream` `false` y el timeout
   `HOST_REPORT_TIMEOUT_SECONDS`. Las redirecciones se rechazan, la verificación
   de TLS nunca se desactiva y no se reintenta nada.
5. **Validación estricta de la respuesta.** La respuesta debe ser exactamente un
   objeto JSON con `summary` y `findings`; cada hallazgo debe tener exactamente
   `container`, `severity`, `what`, `why` y `action`; los contenedores deben ser
   nombres presentes en la evidencia renderizada; las severidades son `high`,
   `medium` o `low`; como máximo 3 hallazgos; el resumen de 300 caracteres como
   máximo y cualquier otro string de 150 como máximo; se rechaza todo string que
   cite un marcador de normalización (`<num>`, `<ts>`). Se desenvuelve una única
   valla markdown alrededor del objeto, porque el modelo desplegado responde
   con ```` ```json ```` incluso cuando se le pide JSON crudo; la prosa antes
   o después de ella, dos vallas o una valla sin cerrar se siguen rechazando. Las
   claves extra en el nivel superior se rechazan. El presupuesto de salida por
   defecto está medido, no adivinado: una respuesta con un hallazgo ronda los 160
   tokens, y un presupuesto más corto trunca el JSON a mitad de objeto, lo que
   falla esta validación y descarta la interpretación.
6. **Los hechos y la interpretación se mantienen separados.** `## Facts` contiene
   solo conteos, estados, reinicios, salidas y totales por contenedor calculados
   por el collector. `## Interpretation` contiene el resumen y los hallazgos, o
   una nota explícita de que la interpretación no está disponible.
7. **Una llamada al modelo fallida nunca pierde el reporte.** Un archivo de clave
   ausente, una URL de gateway ausente, un error HTTP, un timeout o una respuesta
   inválida producen el reporte solo con hechos, con el motivo registrado en los
   metadatos, y salida `0`. Con `--strict-model` la salida es `4` en cambio (sin
   reporte en stdout).

Los prompts delimitan el texto recolectado entre `<<<EVIDENCE` y `EVIDENCE>>>` e
instruyen al modelo para que lo trate como datos no confiables, nunca como
instrucciones.

## Entrega

Un reporte markdown es más grande que un mensaje de WhatsApp, así que el job
envía un resumen compacto en su lugar y deja el reporte completo en stdout para
el log del programador:

```
🖥️ Servidor: server · 24h
📊 18715 líneas · 620 errores · 2 reinicios

Un contenedor falló al consultar el estado de sus servicios.

1. 🔴 dockge-dockge-1 (alta): Salió con código 1.
   ↳ Por qué: La consulta de estado no alcanza su servicio.
   ↳ Acción: Revisar la variable de entorno que falta.
```

El mensaje va en español con un color por severidad (🔴 high, 🟠 medium, 🟡 low)
y una ventana tranquila dice `✅ Sin novedades`; el prompt le pide al modelo
strings en español mientras las claves JSON y los valores de severidad siguen en
inglés, así el validador nunca interpreta etiquetas traducidas.

- **El hub es el dueño de WhatsApp.** El job nunca tiene una credencial de WAHA;
  lee un token bearer de un archivo y el hub resuelve el rol a su identidad.
- **Un reintento nunca envía dos veces.** Cada mensaje lleva una clave de
  deduplicación y el hub la reclama antes de llamar a WAHA. La clave por defecto
  es el host más el fin de la ventana al minuto
  (`host-report:<host>:<YYYY-MM-DDTHH:MM>`), así un programador que reintenta el
  mismo slot está a salvo; `HOST_REPORT_NOTIFY_KEY` permite que el programador
  pase su propio id de slot.
- **Las ejecuciones sin novedades también se envían.** Cuando el umbral de
  omisión deja al modelo afuera, el mensaje lo dice en una línea junto a los
  totales deterministas. Un reporte silencioso deliberado nunca se confunde, así,
  con un job que murió.
- **Una llamada, sin reintentos.** Las redirecciones se rechazan, la verificación
  de TLS nunca se desactiva, un mensaje más largo de lo que acepta el hub se
  rechaza antes de enviar nada, y una entrega fallida sale con `5` después de que
  el reporte ya se escribió, así no se pierde nada. stderr solo lleva
  `host_report: hub status=<code>
  elapsed=<seconds>s` y `host_report: hub result=sent|duplicate`.

## Metadatos del reporte

El reporte markdown empieza con un comentario HTML para que el reporte siga
siendo legible:

```
<!-- host_report: {"host":"...","model":{"status":"used|skipped|unavailable","reason":null},"prompt":{"budget":3200,"chars":958,"containers":["api"],"drops":[]},...} -->
```

`prompt.drops` lista cada descarte por presupuesto en orden (`kind` es `cluster`
o `container`). La clave de la aplicación, el contenido del archivo de CA y la
respuesta cruda del gateway nunca aparecen en el reporte ni en stderr; stderr
solo lleva el código de estado del gateway y el tiempo transcurrido, por ejemplo
`host_report: gateway status=200 elapsed=0.41s`.

## Topes

Los topes siempre quedan registrados en `truncations`, nunca se aplican en
silencio, y un tope nunca esconde la señal: las líneas de error y de advertencia
se retienen antes que las informativas.

- **Líneas**: se analizan como máximo `max_lines` líneas por contenedor. A Docker
  se le pide `tail = max_lines * HOST_REPORT_OVERFETCH` (por defecto `4`),
  acotado a `20000`, así una línea de error muy abajo del stream todavía puede
  conservarse; un log enorme nunca llega a memoria porque el lector se detiene en
  ese tail. Mientras se consumen las líneas, se conserva primero cada línea
  `error`, después cada línea `warning` y después las informativas en el orden del
  stream, hasta llenar el tope. Las líneas retenidas se analizan en el orden del
  stream, así el clustering y el orden no cambian cuando no se alcanza el tope.
  Dos registros describen lo que pasó: `kind: "lines"` dice que se alcanzó el
  tope, y `kind: "sampling"` lleva `lines_read`, `kept`, `errors_kept`,
  `warnings_kept`, `errors_dropped`, `warnings_dropped` e `info_dropped`. Un
  contenedor muestreado, por lo tanto, nunca es indistinguible de uno limpio.
  Cuando el stream fue más largo que el tail pedido, el lector registra
  `kind: "lines"` con la nota de que el resto no se leyó.
- **Clusters**: como máximo `max_clusters` clusters por contenedor, conservando
  los de mayor conteo (los empates se resuelven por primera aparición).
- **Contenedores**: como máximo `--max-containers` contenedores, tomados en orden
  de nombre. El resto se lista en `skipped` con un motivo.

## Secuencias de escape ANSI

Los logs con color se limpian lo antes posible: justo después de decodificar la
línea y quitarle su prefijo de timestamp de Docker, y antes de los patrones de
ignore, la clasificación de severidad, el enmascarado, el clustering y la salida.
Se eliminan las secuencias CSI (`ESC [ ... final byte`, o sea `\x1b[31m`), las
secuencias OSC (`ESC ] ... BEL` o `ESC ] ... ESC \`) y los escapes de dos
caracteres `ESC x`. Solo se eliminan bytes ESC reales: los parecidos textuales,
como los caracteres literales `\u001b[31m` o `^[[31m` dentro de un mensaje, se
preservan, y el contenido UTF-8 no ASCII sobrevive. La limpieza es idempotente y
nunca lanza excepciones; una secuencia cortada al final de una línea se descarta
con su cola incompleta y nunca se come la línea siguiente.

## Esquema del digest

```json
{
  "schema_version": 1,
  "host": "hostname",
  "window": {"since": "2024-05-01T10:00:00Z", "until": "2024-05-02T10:00:00Z", "duration": "24h"},
  "containers": [
    {
      "name": "api",
      "image": "example/api:1",
      "state": "running",
      "restart_count": 1,
      "exit_code": null,
      "lines_read": 42,
      "ignored_lines": 2,
      "total_lines": 40,
      "error_lines": 3,
      "warning_lines": 1,
      "clusters": [
        {
          "template": "upstream request failed after <duration>",
          "count": 3,
          "first_seen": "2024-05-01T10:00:01.000000Z",
          "last_seen": "2024-05-01T11:42:07.500000Z",
          "severity": "error",
          "sample": "upstream request failed after 1.5s"
        }
      ]
    }
  ],
  "skipped": [{"container": "db", "reason": "GET /containers/<id>/logs returned HTTP 404"}],
  "truncations": [
    {
      "container": "api",
      "kind": "lines",
      "limit": 2000,
      "observed": 2001,
      "message": "line cap reached; the rest of the log stream was not read"
    },
    {
      "container": "noisy",
      "kind": "sampling",
      "limit": 2000,
      "observed": 8000,
      "message": "line cap reached; some informational lines were not analysed",
      "lines_read": 8000,
      "kept": 2000,
      "errors_kept": 375,
      "warnings_kept": 12,
      "errors_dropped": 0,
      "warnings_dropped": 0,
      "info_dropped": 5613
    }
  ]
}
```

- `containers` está ordenado por `error_lines` descendente y después por `name`.
- `lines_read` cuenta cada línea consumida del stream; `ignored_lines` cuenta las
  descartadas por los patrones de ignore; `total_lines` es el remanente
  analizado, así que `total_lines <= lines_read - ignored_lines` (igualdad salvo
  que el tope de líneas haya forzado muestreo, que es lo que describe el
  registro `sampling`). `error_lines + warning_lines + rest == total_lines`.
- `exit_code` solo se reporta para contenedores que no están `running`; el
  `ExitCode` de un contenedor en ejecución no dice nada sobre su futuro.
- `first_seen`/`last_seen` vienen del prefijo `timestamps=1` de Docker y son
  `null` cuando una línea no trae ningún timestamp.
- `kind` es `lines`, `clusters`, `containers` o `sampling`; `container` es `null`
  para los topes a nivel de host. `limit` es el tope que se disparó y `observed`
  el conteo visto, así los mismos campos se renderizan para todos los tipos.

## Clustering

Las líneas que difieren solo en partes volátiles colapsan en un único cluster con
clave de plantilla normalizada. Marcadores reemplazados, en este orden: `<ts>`
(timestamps ISO), `<id>` (UUIDs), `<hex>` (`0x…` e ids hexadecimales de 8+
caracteres), `<str>` (strings entre comillas, incluidas rutas entre comillas),
`<path>` (rutas absolutas sin comillas), `<duration>` (`1.5s`, `200ms`, `3m`),
`<num>` (los números restantes). Los espacios en blanco se colapsan. La severidad
es la más alta entre las líneas del cluster: `error` para texto de
error/fail/fatal/exception/panic/critical, `warning` para texto de
warn/deprecated, `info` en el resto. Cada cluster conserva una línea de muestra
truncada a 200 caracteres.

## Enmascarado

El texto de log es dato no confiable: nunca se evalúa, nunca se pasa a un shell y
nunca se interpola en un comando. Los valores con aspecto de credencial se
enmascaran en cada línea antes del clustering y otra vez en la plantilla y la
muestra salientes, así nada sale del proceso sin enmascarar. El enmascarado
conserva la clave y reemplaza solo el valor por `[redacted]`:

- `password=…`, `passwd=…`, `token=…`, `secret=…`, `api-key=…`, `api_key=…`
  (consciente de comillas, para las formas `key=value` y `"key": "value"`)
- `Authorization: Bearer …`
- `X-Api-Key: …`
- blobs largos (32+ caracteres) dentro del valor de un parámetro de URL o
  como valor completo de una línea de cabecera `name: value`
- identificadores hexadecimales largos (32+ caracteres hex) y tokens largos
  tipo base64 (32+ caracteres que contienen algún dígito; las barras quedan
  excluidas para que las rutas de archivo sobrevivan)

## Tests

`tests/test_host_report_collector.py`, `tests/test_host_report_report.py` y
`tests/test_host_report_noise.py` ejercitan la CLI del collector contra un doble
local de `ThreadingHTTPServer` (`tests/host_report_fake.py`) que habla los tres
endpoints de Docker, incluido un cuerpo multiplexado real y uno plano. Los tests
de ruido cubren la limpieza de ANSI (CSI, OSC, dos caracteres, parecidos
textuales, secuencias truncadas, UTF-8), el muestreo consciente de la severidad
con sus conteos registrados, y los topes por contenedor incluidos los valores
rechazados de `HOST_REPORT_CONTAINER_LIMITS`. `tests/test_host_report_writer.py`
ejercita el reporte markdown contra el mismo doble de Docker más un doble local
de gateway del mismo módulo: render acotado, descartes registrados, salida por
presupuesto excedido, umbral de omisión, payload exacto de la petición,
validación estricta de la respuesta, todos los fallbacks solo con hechos y la
ausencia de la clave y del cuerpo crudo de la respuesta en stdout y en stderr.
Sin red, sin daemon de Docker, sin listener TLS y sin secretos.
