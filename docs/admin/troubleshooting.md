# Troubleshooting

## The page says "Cubicle is served at …"

You opened the server by a different name or address than `SITE_ADDRESS`. Open the address it names.

## The browser warns about the certificate

Caddy's own certificate authority issued it. Install its root certificate on the machine
([Installing → HTTPS on a LAN](install.md#https-on-a-lan)), or use an `http://` address with
`AUTH_COOKIE_SECURE=0`.

## A reply is an error from the model server

The server's error is shown as the reply. The usual causes:

- **401 or 403**: the key is missing or wrong. Set it in App Settings → Model server.
- **404 on `/chat/completions`**: the base URL is wrong; for local servers it ends in `/v1`.
- **Unknown or unloaded model**: the model isn't one the server offers. Refresh the model picker, or
  load the model on the server first.
- **`reasoning_effort` not recognized**: set Reasoning effort to "Not sent" in App Settings, and the
  bot's Thinking to Default.
- **Could not reach …**: the server isn't running or isn't listening there. From inside the API
  container, `localhost` is the container; see
  [A model server on the same machine](install.md#a-model-server-on-the-same-machine).

## The model picker is empty

The server didn't answer `GET /models`. Type the model's id in the picker's search box and choose it.

## A bot ignores images

The model has to accept images. Pick a vision model, or check that your server has its vision part
loaded.

## A reply says "Waiting for a model slot"

More requests are running at the model server than **Requests at a time** allows (App Settings → Model
server). It starts when one finishes; the chat shows its place in line.

## The computer won't start

- Check that `COMPUTER_PROVIDER=docker` and `COMPOSE_PROFILES=computer` are set, and that the runtime
  image exists: `docker image inspect cubicle-computer:2.14.0`.
- The API reports a failed start instead of quietly carrying on without a computer; its log says why:
  `docker compose logs api`.
- If the image was built on Windows from a checkout with CRLF line endings, its scripts can't run (the
  container exits at once with code 127). The repository's `.gitattributes` prevents this for new
  checkouts; rebuild the image from one.

## The Files tab says files can't be listed

The file viewer needs the computers' workspaces on the host: `COMPUTER_DOCKER_WORKSPACE_MODE=bind` and
`COMPUTER_DOCKER_HOST_WORKSPACE_ROOT` (see [the computer runtime](install.md#the-computer-runtime)).

## Auto-approval or Auto thinking does nothing

Both need the decision server. Check App Settings → Auto-approval → **Test**. With no decision server,
every approval asks you and Auto falls back to the level the conversation last used.

## Locked out

Use the command line in the API container to set a password, switch off two-factor, or make a token:
see [Accounts from the command line](install.md#accounts-from-the-command-line).
