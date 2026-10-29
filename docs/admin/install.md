# Installing Cubicle

Cubicle runs as a few containers: the API (FastAPI), the web client (Next.js), and Caddy in front of
both on one address. Optionally, each bot gets a sandboxed computer, also a container.

## What you need

- A machine with Docker and Docker Compose (Linux, a NAS like Unraid or TrueNAS, or Docker Desktop).
- A model server that speaks the OpenAI API: a local one (halogen-flash-server, llama.cpp, Ollama,
  LM Studio, vLLM) or a hosted one (OpenRouter, OpenAI). Any server with `POST /chat/completions` and
  streaming works; if it also has `GET /models`, the model picker lists its models.

## Start it

```bash
git clone https://github.com/cannibal3004/cubicle.git
cd cubicle
cp .env.example .env
# edit .env: set SITE_ADDRESS to the address people will open, e.g. https://192.168.1.20
docker compose up --build -d
```

Open the `SITE_ADDRESS`. The first visit shows a setup page that creates the owner's account; after that
it is a sign-in page. Then set the model server in **App Settings → Model server** (or as `LLM_BASE_URL`
and `LLM_API_KEY` in `.env`): see [App Settings for admins](settings.md).

Caddy serves the app only for requests whose hostname or IP matches `SITE_ADDRESS` (the port may
differ); opening it by another name shows a short message saying which address to use.

### HTTPS on a LAN

With an `https://` address, Caddy issues a certificate from its own certificate authority for that
hostname or IP, so browsers warn until they trust its root certificate. Export it once and install it on
each machine that uses Cubicle:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./cubicle-ca.crt
```

HTTPS is also what lets voice dictation work from another machine. To skip certificates, set
`SITE_ADDRESS=http://<hostname-or-ip>` and `AUTH_COOKIE_SECURE=0`.

### A model server on the same machine

Inside the API container, `localhost` is the container. A server on the Docker host is
`http://host.docker.internal:<port>/v1`; one on another machine is reached by its LAN address. The same
goes for the decision server used by auto-approval and Auto thinking (`DECIDER_URL`, without `/v1`).

## The computer runtime

Bots get their own sandboxed desktop when the computer runtime is on. It needs the API to start
containers, so it is off by default. To turn it on, add to `.env` and bring the stack up again:

```bash
COMPUTER_PROVIDER=docker
COMPOSE_PROFILES=computer
```

This builds the runtime image (`cubicle-computer`) and starts a socket proxy on an internal network: the
API never sees the Docker socket, and the proxy allows only the container operations the computers use.
Each computer joins a dedicated `cubicle-computers` network and publishes no ports; you watch it through
the API, which checks your session.

Each computer has a workspace (`/workspace`), a home directory and its list of installed packages,
which survive restarts and image rebuilds. By default the workspaces are Docker volumes. To keep them on
the host (so you can back them up, and so the [file viewer](../computer.md#files) works), set
`COMPUTER_DOCKER_WORKSPACE_MODE=bind` and `COMPUTER_DOCKER_HOST_WORKSPACE_ROOT` to the host path of the
API's `/data/computers` (with `API_DATA_DIR=/srv/cubicle/data`, that is `/srv/cubicle/data/computers`).

Computers are started by the API, not by compose, so `docker compose down` does not stop ones that are
running. Stop them from the UI first, or remove them by label:

```bash
docker rm -f $(docker ps -q --filter label=cubicle.runtime=computer)
```

The computer is a sandbox for a bot's work, not a hardened browser for hostile sites: it has internet
access, and anything signed in there is reachable by the bot.

## The shared folder

`WORKSPACE_DIR` (default `./workspace`) is the [shared folder](../files.md#the-shared-folder): the
directory you and every bot can reach. With an absolute path, it is also mounted at `/shared` in every
bot's computer (compose cannot hand a relative path to Docker; set `COMPUTER_DOCKER_HOST_SHARED_DIR` to
the absolute path instead). The API and the computers run as different users, so make the folder
writable by both (`chmod 777` on it is the simplest way).

## Data and backups

The API keeps everything under `/data`: the SQLite database (accounts, bots, conversations, settings
with encrypted keys, approvals, audit) and the encryption key, plus the computers' workspaces in bind
mode. Caddy keeps its certificate authority under its own `/data`. Both are Docker volumes by default;
to keep them in a directory your backups cover, set `API_DATA_DIR` and `CADDY_DATA_DIR` in `.env` to host
paths. The API runs as uid 10001, so make its directory writable by that user
(`chown -R 10001:10001 <dir>`). Back up the database and the key together: without the key, stored
credentials have to be entered again.

## Updating

```bash
git pull
docker compose up -d --build
```

If you run the computer runtime, rebuild its image too (`docker compose --profile computer build`);
computers pick up the new image at their next start and keep their files and packages.

## Accounts from the command line

`python -m app.cli` inside the API container manages accounts when you are locked out of the UI:

```bash
docker compose exec -T api python -m app.cli users
docker compose exec -T api python -m app.cli create-user <name>        # password on stdin
docker compose exec -T api python -m app.cli set-password <name>       # password on stdin
docker compose exec -T api python -m app.cli disable-totp <name>
docker compose exec -T api python -m app.cli create-token <name> <label>
```

## Running without Docker

For development, run the API and the client directly:

```bash
cd server && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py                      # http://127.0.0.1:8000, API docs at /docs

cd client && npm install && npm run dev   # http://localhost:3000
```

The client calls `http://127.0.0.1:8000/api/v1` unless `NEXT_PUBLIC_API_URL` says otherwise.
