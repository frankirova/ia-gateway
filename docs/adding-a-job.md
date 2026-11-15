# Agregar un job

Ya existen tres servicios y ningún job los cambia. Un job es solo el código que
mide sus sistemas y pide una interpretación.

Este documento es la spec: léelo antes de crear el primer archivo de un job.

| Servicio | Es dueño de | Un job nunca |
| --- | --- | --- |
| Gateway (`https://gateway.internal`) | La inferencia, las listas de modelos permitidos, las claves por aplicación y los límites de tasa | Hablar con llama.cpp ni guardar su dirección |
| Hub (`POST /send`) | La sesión de WhatsApp, la entrega y la deduplicación | Guardar una credencial de WAHA |
| n8n | La programación y la invocación | Contener lógica de reportes |

## Los cuatro verbos

1. **Medir (Measure)** — un script recolecta hechos: conteos, fechas, tamaños,
   códigos de salida, reinicios, porcentajes de disco. Determinista, testeable
   sin un modelo.
2. **Interpretar (Interpret)** — una llamada al gateway sobre un prompt acotado.
   El modelo ordena y explica; nunca calcula un número que el script ya conoce.
3. **Entregar (Deliver)** — una llamada al hub con un mensaje compacto y una
   clave de deduplicación. El reporte completo queda en stdout para el log del
   programador.
4. **Registrar (Record)** — opcional. El número de ayer se guarda solo cuando el
   job deba comparar contra el pasado («el archivo es un 40 % más pequeño de lo
   habitual»).

Cuando la respuesta es un número o un sí/no, se omite el paso 2: el script ya lo
sabe.

## Paso 1: el código del job

Se crea `tasks/<job>/` y se reutilizan los módulos que no tienen que ver con los
logs del host:

| Módulo | Reutilizable | Específico del job |
| --- | --- | --- |
| `gateway.py` | una llamada autenticada, TLS verificado, sin reintentos | — |
| `interpretation.py` | validación estricta de la respuesta, un bloque delimitado desenvuelto | la forma JSON que valida |
| `delivery.py` | mensaje compacto, clave de deduplicación, una llamada al hub | los campos del mensaje |
| `writer.py` | umbral de salto, fallback solo con hechos, códigos de salida | qué cuenta como "actividad" |
| `collector.py`, `docker_api.py`, `clustering.py`, `ansi.py` | — | cómo mide este job |

Hoy los archivos viven en `tasks/host_report/`. El segundo job es el momento de
extraer los reutilizables a un paquete compartido en lugar de importar entre
jobs; hasta entonces, no se copia nada y se espera el segundo uso.

Se mantiene la misma interfaz en todos los jobs: `python -m tasks.<job>`, código de
salida `0` reporte escrito, `1` una fuente no es alcanzable, `2` configuración
incorrecta, `3` prompt por encima del presupuesto, `4` el modo estricto no obtuvo
interpretación, `5` la entrega falló. stderr lleva solo diagnósticos con códigos
de estado y tiempos transcurridos.

## Paso 2: la aplicación en el gateway

Cada job tiene su propia entrada de aplicación, así que revocar un job no puede
romper otro. La configuración del gateway toma una lista de aplicaciones:

```json
{
  "applications": [
    {
      "id": "host-report",
      "key_sha256": "<64 hex characters: sha256 of the job's key>",
      "models": ["e4b"],
      "max_output_tokens": 400,
      "rate_capacity": 4,
      "rate_per_minute": 1.0
    }
  ]
}
```

El job lee su clave desde un archivo, nunca desde un flag ni desde un valor de
entorno:

```text
HOST_REPORT_GATEWAY_KEY_FILE=/etc/host-report/gateway.key
HOST_REPORT_GATEWAY_CA_BUNDLE=/opt/stacks/gateway/ca/caddy-root.crt
```

`max_output_tokens` debe coincidir con lo que pide el job: una respuesta truncada
a mitad de JSON falla la validación y degrada el reporte en silencio a solo
hechos.

## Paso 3: la entrega

```text
HOST_REPORT_HUB_URL=http://192.0.2.10:8000
HOST_REPORT_HUB_TOKEN_FILE=/etc/host-report/hub.token
HOST_REPORT_HUB_CHAT=owner
HOST_REPORT_NOTIFY_KEY=<scheduler slot, optional>
```

La clave de deduplicación es el contrato con el hub: el hub la reclama antes de
llamar a WAHA, así que una ejecución reintentada responde `duplicate` y no envía
nada dos veces. Se deriva del slot de programación, nunca de un valor aleatorio —
una clave aleatoria la anula.

## Paso 4: la programación

Un workflow por job en `deploy/n8n/<job>.json`: un nodo `Schedule` y un nodo
`SSH` que pide el comando forzado (forced command). El workflow se importa, la
credencial se vincula y el workflow se habilita solo después de que una ejecución
manual devuelva `0`.

El comando forzado (`deploy/host-report/host-report-run`) ignora
`SSH_ORIGINAL_COMMAND`, así que el cliente no elige ni la ventana, ni el
destinatario, ni la clave. Se copia ese patrón para un job nuevo:
`restrict,command=` en `authorized_keys` más una cuenta que solo puede ejecutar
es lo que hace que una clave de n8n robada valga solo un reporte.

## Un segundo host que solo reporta

Un host sobre el que no se controla la programación no necesita su propia
programación, ni una clave para el gateway, ni una clave para el hub. Ese host se
convierte en un sujeto del reportero que ya tiene:

- `HOST_REPORT_TARGET=ssh` más `HOST_REPORT_SSH_TARGET` lee los contenedores y el
  estado de ese host a través de una única llamada SSH, y `HOST_REPORT_ENDPOINTS`
  sondea sus URLs desde el reportero — un chequeo de adentro hacia afuera no
  puede decirle que el servicio es inalcanzable desde afuera.
- El sujeto ejecuta un único programa fijo de solo lectura detrás de
  `restrict,command=` (`deploy/vps-report/`), con su propia clave dedicada para
  que revocar el reporte no revoque un backup y lo inverso también sea cierto.
  Una segunda entrada restringida para la misma clave pública no funciona: sshd
  aplica la primera entrada que coincide.
- Cada host reportado necesita su propio comando forzado (`vps-report-run`) y su
  propio archivo de entorno (`/etc/host-report/vps.env`), porque la clave de
  deduplicación debe nombrar al host reportado y el wrapper corre en el reportero,
  donde `hostname -s` es la respuesta incorrecta.
- Un hecho medido se enuncia, no se interpreta: disco, memoria, carga y estado
  del endpoint se renderizan antes que cualquier evidencia de logs para que el
  presupuesto del prompt nunca pueda descartarlos, y el modelo sigue
  interpretando solo los logs de contenedores.

Lo que cuesta el transporte, escrito para que nadie se sorprenda: el sujeto no
toma ningún parámetro, así que la cobertura de logs es el techo remoto de
`--tail` y el reportero solo puede descartar líneas más antiguas que su ventana
después de leer su marca de tiempo. El reporte lo dice en sus propias
limitaciones en lugar de ocultar una ventana corta.

## Política de claves

- Una clave por integración, una aplicación de gateway por job, una entrada en
  `authorized_keys` por programador.
- El lado que necesita los datos inicia. Una máquina que se reporta a sí misma no
  necesita ninguna clave entrante.
- Ninguna clave privada se copia entre máquinas; cada par se queda donde se usa.
- Toda clave entrante está restringida: `restrict,command=`, una cuenta sin
  shell, nunca `root`.
- Una máquina caída se detecta por la **ausencia** de su reporte, no por un
  sondeo SSH desde otro lugar.

## Criterios de terminado

- `uv run pytest -q` en verde, con una prueba por cada efecto observable que el
  job afirma.
- Una ejecución real sin overrides de ajuste: código de salida `0`, y el mensaje
  legible en un teléfono.
- Una segunda ejecución en el mismo slot responde `duplicate` y no envía nada.
- `odd/tasks/<job>.md` registra las specs, las tareas y la evidencia, y su
  `## Log` guarda las decisiones del humano de forma literal.
