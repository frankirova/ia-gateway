# Despliegue del gateway

Destino: el servidor del laboratorio `192.0.2.10`, junto al contenedor
llama.cpp al que sirve.

## Forma

```text
client → https://gateway.internal (Caddy, internal CA) → 192.0.2.10:8099 → gateway container → llama-server:8080
```

- El gateway es el único punto de entrada autenticado. El backend permanece
  privado e inalcanzable desde los clientes de tareas.
- El alias público es `e4b`; el nombre del modelo de origen nunca sale del
  gateway.

## Archivos en el servidor

| Ruta | Modo | Contenido |
| --- | --- | --- |
| `/opt/stacks/gateway/compose.yaml` | `0644` | el `deploy/gateway/compose.yaml` de este repositorio |
| `/opt/stacks/gateway/settings` | `0600` | archivo de entorno que fija `GATEWAY_IMAGE` a un digest `sha256:` inmutable |
| `/opt/stacks/gateway/config/gateway.json` | `0644` | configuración del gateway; solo digests, nunca una clave |
| `/opt/stacks/gateway/application-key` | `0600` | la clave de aplicación que coincide con el digest de la configuración |

El directorio de configuración se monta en solo lectura, así que el archivo debe
ser legible por todos dentro del contenedor (`0644`). Eso es seguro porque
`key_sha256` es un digest. La clave en sí nunca entra en el sistema de archivos
del contenedor.

## Desplegar un cambio

```bash
tar -czf - app deploy/gateway/Dockerfile \
  | ssh server@192.0.2.10 'rm -rf /tmp/gw-build && mkdir -p /tmp/gw-build \
      && tar -xzf - -C /tmp/gw-build && cd /tmp/gw-build \
      && docker build -q -f deploy/gateway/Dockerfile -t gateway:local .'
```

Luego se fija el nuevo digest en `/opt/stacks/gateway/settings` y se recrea el
servicio:

```bash
cd /opt/stacks/gateway
sed -i "s|^GATEWAY_IMAGE=.*|GATEWAY_IMAGE=$(docker image inspect -f '{{.Id}}' gateway:local)|" settings
docker compose --env-file settings up -d
```

Se conserva el digest anterior para revertir; la imagen antigua permanece en el host.

## Caddy

El bloque de sitio vive en `/opt/stacks/caddy/Caddyfile`:

```caddyfile
https://gateway.internal {
	tls internal
	reverse_proxy 192.0.2.10:8099
}
```

`tls internal` es obligatorio: de lo contrario Caddy intenta ACME público y falla
con `rejectedIdentifier`, porque `.internal` no es un sufijo público. Se valida y se
recarga en lugar de reiniciar:

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec caddy caddy reload   --config /etc/caddy/Caddyfile --adapter caddyfile
```

## Requisitos previos que aquí no están automatizados

1. **DNS**: un registro para `gateway.internal` que apunte a `192.0.2.10`. Mientras
   no exista, solo funciona `curl --resolve gateway.internal:443:192.0.2.10 https://gateway.internal/...`.
2. **Confianza del cliente**: el certificado proviene de la CA interna de Caddy.
   Los jobs deben validarlo y nunca desactivar la verificación. Instalar la CA en
   el almacén de confianza del sistema requiere root, algo que este despliegue no
   asume; en su lugar la CA se exporta una vez a
   `/opt/stacks/gateway/ca/caddy-root.crt` (`0644`) y cada invocador apunta a ella:

```bash
curl --cacert /opt/stacks/gateway/ca/caddy-root.crt https://gateway.internal/health/live
# Python requests/httpx: verify="/opt/stacks/gateway/ca/caddy-root.crt"
```

   La CA se exporta después de cualquier rotación:

```bash
docker exec caddy cat /data/caddy/pki/authorities/local/root.crt \
  > /opt/stacks/gateway/ca/caddy-root.crt
chmod 644 /opt/stacks/gateway/ca/caddy-root.crt
```

## Advertencia sobre el modelo de razonamiento

El modelo configurado (`gemma-4-E4B-it-UD-Q4_K_XL`) es un modelo que razona.
llama.cpp devuelve su razonamiento en `reasoning_content` y solo rellena `content`
cuando termina, así que un `max_tokens` pequeño produce una respuesta vacía, que
el gateway reporta correctamente como `502 upstream_error`.

Medido en este host (solo CPU): un prompt trivial consumió 182 tokens de respuesta
y tardó unos 105 s, así que el `read_timeout` desplegado es de 240 s. Los prompts
de análisis reales necesitan un `max_tokens` generoso y un tiempo límite acorde.
No se reduce ninguno de los dos sin medir.
