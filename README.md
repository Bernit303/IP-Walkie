# LAN Walkie

A push-to-talk walkie-talkie that runs on your own network. Open the page, pick a name, hold the button, talk. Everyone else on the page hears you.

No accounts, no cloud storage. Audio goes peer-to-peer over WebRTC; the server only handles the initial handshake and decides who's allowed to talk.

## Why HTTPS

Browsers refuse microphone access on plain `http://` unless the address is `localhost`. That includes your own LAN. The server generates a self-signed certificate automatically on first run, covering `localhost`, the machine's hostname, and every LAN IP it detects. Each device still needs to click through one browser warning the first time it connects — self-signed certs always trigger that, there's no way around it without a real CA — but it's a one-time thing per device, not per session.

## Run it directly with Node

```bash
cd lan-walkie
npm install
npm start
```

You'll see:

```
LAN Walkie running on https://<optibox-ip>:8443
```

From any phone or laptop on the same network, go to `https://192.168.1.3:8443` (swap in optibox's actual IP). Click "Advanced" then "Proceed" through the cert warning. After that it just works.

## Run it as a Portainer stack

This is the recommended way to run it alongside your other self-hosted services.

1. In Portainer, go to **Stacks → Add stack**.
2. Name it `lan-walkie`.
3. Either paste the contents of `docker-compose.yml` into the web editor, or point Portainer at a git repo containing this project (Portainer's "Repository" build method), since building an image from a Dockerfile needs Portainer to actually have the source, not just the compose file.
4. Deploy.

The compose file:

```yaml
services:
  lan-walkie:
    build: .
    image: lan-walkie:latest
    container_name: lan-walkie
    restart: unless-stopped
    ports:
      - "8443:8443"
    volumes:
      - lan-walkie-certs:/app/certs
    environment:
      - PORT=8443
      - CERT_HOSTNAMES=walkie.home,optibox
      - CERT_IPS=192.168.1.3
      - MAX_TRANSMIT_MS=60000

volumes:
  lan-walkie-certs:
```

**Why `CERT_HOSTNAMES`/`CERT_IPS` matter here:** the server generates its cert by auto-detecting its own hostname and network interfaces. Run directly with `node server.js`, that correctly sees optibox's real LAN identity. Run inside Docker, it instead sees the *container's* internal identity — something like `172.18.0.2` — because that's genuinely what the container's network namespace looks like from the inside. The cert would be valid for an address nobody ever visits. Setting `CERT_IPS` to optibox's actual LAN IP (and `CERT_HOSTNAMES` to whatever name you use to reach it) fixes that. Delete the `lan-walkie-certs` volume and restart if you change these after the first run — the cert is generated once and cached.

`MAX_TRANSMIT_MS` (default 60000) is a safety net: if something holds the floor and never sends a release — a frozen tab, a dead battery, a crashed headless client — the server force-releases it after this many milliseconds so the channel can't get stuck.

`PRIORITY_TOKEN` (unset by default, meaning priority is off entirely) — a shared secret. A client that joins with a matching `token` can interrupt whoever's currently talking instead of being denied. See "Headless client" above and `cli/README.md` for how a CLI client supplies it.

The named volume keeps the same certificate across redeploys, so devices don't have to re-accept the warning every time you update the stack.

If you'd rather build the image yourself first and skip giving Portainer the source:

```bash
docker build -t lan-walkie .
docker run -d --name lan-walkie -p 8443:8443 -v lan-walkie-certs:/app/certs lan-walkie
```

then reference `image: lan-walkie:latest` in a stack without a `build:` line.

## Hosting it from a private git repo (for Portainer)

Portainer's git-based stacks need somewhere to pull the Dockerfile from. To
keep it private:

1. Create a new **private** repository (GitHub, GitLab, your own Gitea —
   whatever you already use).
2. Push this project to it:
   ```bash
   cd lan-walkie
   git init
   git add .
   git commit -m "LAN Walkie"
   git remote add origin git@github.com:yourname/lan-walkie.git
   git push -u origin main
   ```
3. In Portainer: **Stacks → Add stack → Repository**.
   - Repository URL: your repo's URL.
   - Since it's private, Portainer needs credentials — either a **Personal
     Access Token** (GitHub: Settings → Developer settings → Fine-grained
     tokens, scoped to just this repo, read-only) or an SSH deploy key,
     depending on which auth method Portainer's version offers you. Paste
     that into the "Authentication" fields on the same screen.
   - Compose path: `docker-compose.yml` (default, since it's at the repo
     root here).
4. Deploy. Portainer clones the repo, builds the image from the
   `Dockerfile`, and starts it.

Every time you `git push` an update, Portainer's stack has a "pull and
redeploy" option to update it — no need to re-paste anything.

## Headless client (no browser / no GUI)

For a second machine — like an Ubuntu Server box with no display — see
`cli/README.md`. It's a full WebRTC participant using GStreamer instead of
a browser, controlled entirely from an SSH terminal: press Space to talk,
Space again to stop.

There's no separate "log in" step to worry about — a headless box never
opens the web page at all. `--name` on the command line *is* the
equivalent of typing a name and clicking join in the browser; the client
connects and identifies itself the moment it starts.

**Priority.** One node — say, the one wired up to a Pico for automated
triggering — can be allowed to interrupt whoever's currently talking
instead of just getting denied. That's not based on the display name
(anyone could type the same name), it's a shared secret:

```bash
PRIORITY_TOKEN=some-long-random-string node server.js
```

Only a client that connects with the matching `--token` gets priority.
Everyone — browser or CLI — sees a ⭐ next to that participant's name in
the peer list, so it's visible which one is the priority node without
guessing. Full details, including the token setup on the CLI side, are in
`cli/README.md`.

## Run it as a systemd service (non-Docker alternative)

```bash
sudo tee /etc/systemd/system/lan-walkie.service << 'EOF'
[Unit]
Description=LAN Walkie
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/lan-walkie
ExecStart=/usr/bin/npm start
Restart=on-failure
User=your-username

[Install]
WantedBy=multi-user.target
EOF

sudo mv lan-walkie /opt/
sudo systemctl daemon-reload
sudo systemctl enable --now lan-walkie
```

## How it works

- **Server** (`server.js`): serves the static page and runs a WebSocket signaling channel. It never touches audio — just passes offers, answers, and ICE candidates between browsers, and decides who currently holds the floor.
- **Client** (`public/app.js`): each browser opens a direct WebRTC connection to every other connected browser (a mesh). Your mic track starts muted; holding the PTT button (or Space bar) unmutes it, releasing mutes it again.
- **Channel lock**: the server tracks one floor holder at a time. Press PTT while the channel's free and you get it — your mic unmutes and everyone streams your audio live. Press it while someone else is talking and you're denied; your button shows their name and your mic stays muted. There's no queue — release and press again once they're done.
- **STUN**: the client currently points at Google's public STUN server. STUN never touches your audio — it's used once, at connection setup, so a browser can tell others which address to reach it at. On a flat home LAN this is often unnecessary. Running the *signaling server* inside Docker doesn't change that calculation — Docker's container networking only affects how browsers reach the signaling server itself; the actual audio negotiation happens browser-to-browser and never touches that container. STUN becomes relevant only if participants end up separated by NAT in a way host candidates alone can't cross. Given everything's ending up reachable on the same effective network, it's worth testing with `iceServers: []` (edit the `config` object in `public/app.js`) before assuming STUN — or a self-hosted `coturn` — is needed at all.
- Works well for small groups — a handful of people. A full mesh gets heavier per connection as more people join; past 6-8 simultaneous participants you'd want a media server (SFU) instead, but for a farm, retreat group, or a few rooms this is plenty.

## Notes

- Everyone needs to reach the server directly (same LAN, or via Tailscale/Twingate/VPN into it).
- Chrome/Firefox remember the mic permission per site, so after the first join it's just: open page, hold button, talk.
