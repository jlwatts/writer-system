# Writer System

A local writing studio. You keep markdown vaults on disk, edit them in the browser, and talk to Gemini about the open document. File history is stored in git, outside the vault. Syncthing can sync the vault to other machines.

The app listens on `http://127.0.0.1:8000`. The first visit asks you to set a password. Later visits use that password and a signed session cookie. The password hash stays in the data directory, not in the vault.

## What it does

- Edit markdown files, with a rendered preview.
- Chat about the open document. Shortcuts in `app/SKILL.md` (`/critique`, `/grammar`, and the others) apply only when a message includes that command. A vault can replace the bundled shortcuts with its own `SKILL.md`.
- Keep a separate todo list per vault.
- Snapshot each vault on a timer, then label or restore an older copy of a file.
- Sync `vault/` with the bundled Syncthing container.

An optional Signal gateway is in `docker-compose.signal.yml`. `writer-start` does not start it.

## Run with Docker

Docker is required. From a checkout of this repo:

```bash
./setup-writer
writer-start
writer-status
```

`setup-writer` creates `.env` if needed and asks for a Gemini API key. It also links `writer-start`, `writer-stop`, `writer-restart`, and `writer-status` into `/usr/local/bin`.

| Command | Effect |
| --- | --- |
| `writer-start` | Build and start the stack |
| `writer-stop` | Stop the stack |
| `writer-restart` | Stop, then start |
| `writer-status` | Show containers and follow the app log |

After start:

- Studio: http://127.0.0.1:8000
- Syncthing: http://127.0.0.1:8384

`app/` is mounted into the container, so edits to the Python, templates, and static files are picked up by the reloading server.

## Configuration

Put secrets in `.env`. That file is gitignored.

| Variable | Purpose | Default |
| --- | --- | --- |
| `GEMINI_API_KEY` | Required. The app builds a Gemini client on import. | |
| `GEMINI_MODEL` | Model name sent to Gemini | `gemini-3.6-flash` |
| `VAULTS_DIR` | Directory that holds the vaults | `./vaults` |
| `AUTH_DIR` | Password hash and chat store | `./data` |
| `HISTORY_DIR` | Git directories for file history | `./history` |
| `COOKIE_SECURE` | Set to `1` so the session cookie is HTTPS-only | unset |
| `SNAPSHOTS_ENABLED` | Set to `0` to turn off the snapshot loop | `1` |
| `SNAPSHOT_INTERVAL_SECONDS` | Time between vault snapshots | `900` |
| `BRANCH_TTL_DAYS` | How long history branches are kept | `30` |

Docker Compose sets `COOKIE_SECURE=1`, `AUTH_DIR=/app/data`, and `HISTORY_DIR=/app/history`, and mounts `./vault` at `/app/vaults`.

## Develop without Docker

Python 3.11 and git are required. Git is used for file history.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="your-key"
uvicorn app.main:app --reload
```

Leave `COOKIE_SECURE` unset when you open the app over plain HTTP.

Tests mock Gemini, so any non-empty key is enough:

```bash
export GEMINI_API_KEY=ci-placeholder-not-a-real-key
pytest
```

Lint uses the settings in `pyproject.toml`:

```bash
pip install ruff==0.17.0 mypy==2.4.0 pydocstyle==6.3.0
ruff check app tests
pydocstyle app tests
mypy app tests
```

GitHub Actions runs pytest, Ruff, pydocstyle, and mypy on every push and pull request.
