# Deployment

## Docker Compose

```bash
docker compose up -d --build
```

`docker-compose.yml` builds the image from the `Dockerfile`, publishes
`8443`, and keeps the generated TLS certificate in the `lan-walkie-certs`
volume so devices don't see a new certificate on every redeploy.

The container runs as the unprivileged `node` user with a read-only root
filesystem, all capabilities dropped, `no-new-privileges`, memory/PID limits
and a healthcheck. The certificate volume is the only writable path.

If you are upgrading from an image that ran as root, the old volume is
root-owned and the server can't read it. Delete the volume (a new certificate
is generated), or fix ownership once:

```bash
docker run --rm -v lan-walkie-certs:/c alpine chown -R 1000:1000 /c
```

### Certificate names

The server generates a self-signed certificate on first run covering
`localhost`, the hostname and detected interface addresses. Inside Docker it
only sees the container's addresses, so set the names you actually use:

```yaml
environment:
  - CERT_HOSTNAMES=walkie.example.lan
  - CERT_IPS=192.168.1.100
```

The certificate is generated once. Delete the volume after changing these.

### Choosing where it listens

`BIND_ADDR` selects the host interface the port is published on. Use a LAN
address to keep it off other networks, or `127.0.0.1` when everything arrives
through a local reverse proxy such as Tailscale Serve.

## Trusted HTTPS with Tailscale Serve

Self-signed certificates make every device click through a warning. If your
devices are on [Tailscale](https://tailscale.com) (with MagicDNS and HTTPS
certificates enabled), run on the host:

```bash
sudo tailscale serve --bg https+insecure://localhost:8443
tailscale serve status
```

Browsers then see a real, auto-renewed certificate at
`https://<host>.<tailnet>.ts.net/`; Tailscale terminates TLS and proxies to
the app's self-signed port on the same machine. WebSockets pass through. Undo
with `tailscale serve reset`.

Set `ALLOWED_ORIGINS=https://<host>.<tailnet>.ts.net` once you've confirmed
the page works through Serve, to stop other sites' pages from opening
WebSockets to it.

## systemd (without Docker)

```ini
[Unit]
Description=LAN Walkie
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/lan-walkie
ExecStart=/usr/bin/npm start
Restart=on-failure
User=lan-walkie
NoNewPrivileges=true
EnvironmentFile=-/etc/lan-walkie.env

[Install]
WantedBy=multi-user.target
```

Put `PRIORITY_TOKEN=...` in `/etc/lan-walkie.env` (mode 600, root-owned).

## Priority token

```bash
openssl rand -hex 24
```

Set it as `PRIORITY_TOKEN` on the server and give the same value to the one
client that should be able to interrupt (the CLI's `--token-file`). Don't put
it in version control or on a command line.
