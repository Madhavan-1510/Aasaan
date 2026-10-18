# Deploying Aasaan

One small server, Docker Compose, Caddy for automatic HTTPS, the SQLite file on a volume, a nightly backup.
No Kubernetes, no serverless, no App Runner (closed to new AWS customers).

**SQLite means exactly one app instance.** Do not scale the `app` service. One 1 vCPU / 1-2 GB server is enough.

Status: the Compose file is validated and the app is tested locally. The Docker image build itself has **not** been run on
the machine this was developed on (Docker Desktop was not running). Do the first build on the server and check step 6.

## 1. Server

Any Linux VPS (Ubuntu 22.04/24.04 is assumed). Open ports **80** and **443** (and 22 for SSH). Nothing else.

```bash
# install Docker Engine + the compose plugin: https://docs.docker.com/engine/install/ubuntu/
sudo usermod -aG docker $USER   # log out and back in
```

## 2. DNS

Create an **A record** for your domain (for example `aasaan.example.com`) pointing at the server's public IP.
Caddy needs this to obtain the HTTPS certificate.

## 3. Get the code and configure

```bash
git clone <your repo url> aasaan && cd aasaan
cp .env.example .env
nano .env
```

Fill in `.env`:

| Variable | Needed | Notes |
|---|---|---|
| `GEMINI_API_KEY` | **yes** | Google AI Studio. Without it the app shows "No AI model is connected" and cannot build notebooks. |
| `AASAAN_DB` | optional | SQLite database path. Default: `data/aasaan.db` (in container: `/app/data/aasaan.db`; on ACA: `/app/local/aasaan.db`). Parent dir created on start. |
| `AASAAN_UPLOADS` | optional | Uploads directory path. Default: `data/uploads` (in container: `/app/data/uploads`; on ACA: `/mnt/files`). Created on start. |
| `AASAAN_DOMAIN` | yes (prod) | Your domain, e.g. `aasaan.example.com`. Unset = `localhost`. |
| `AASAAN_ADMIN_TOKEN` | **yes (prod)** | Long random string. Protects `/api/events` and `/api/admin/*`. Without it those are localhost-only. |
| `SARVAM_API_KEY` | optional | Indian-language chat, voice, translation. |
| `TYPESAFE_API_KEY` | optional | Jev: grading misconceptions and ranking resources. |
| `YOUTUBE_API_KEY` | optional | Video search. Without it (and without Gemini grounding) chapters have no videos. |
| `JINA_API_KEY`, `FIRECRAWL_API_KEY`, `OPENVERSE_CLIENT_ID`+`_SECRET` | optional | Firecrawl is the last page tier and a discovery fallback, capped per notebook; Openverse credentials raise image-search limits. |
| `AASAAN_CONTACT` | recommended | A URL or email put in the crawler's User-Agent so site owners can reach you. |
| `AASAAN_NB_PER_HOUR` | optional | Notebooks one IP may create per hour (default 12). |
| `AASAAN_CHAPTER_CONCURRENCY` | optional | Chapters built at once (default 2). Lower it if you hit rate limits. |
| `AASAAN_MAX_CALLS` | optional | Global cap on simultaneous model calls (default 4). |

Generate a token with: `openssl rand -hex 24`.

## 4. Start

```bash
docker compose up -d --build
docker compose ps          # app, caddy and backup should be "running"/"healthy"
```

## 5. Check the models (important)

```bash
curl -s https://YOUR_DOMAIN/api/health/models | python3 -m json.tool
docker compose logs app | grep aasaan | head -30
```

Every model must say `"status": "ok"`. A `failed` entry carries the exact error (wrong key, wrong model id, quota).
The app does **not** silently fall back: each fallback is recorded with its reason in the logs and in the `events`
table (`curl -H "X-Admin-Token: $TOKEN" https://YOUR_DOMAIN/api/events`).

## 6. Smoke test

Open `https://YOUR_DOMAIN`, type "I want to learn DSA". The notebook should open within about a minute with a
roadmap and graph, and the remaining chapters should fill in. If the image fails to build or the container is
unhealthy: `docker compose logs app`.

## Backups

### Docker Compose
The `backup` service writes `./backups/aasaan-YYYY-MM-DD.db` (SQLite online backup, safe while running) at start and
every 24 hours, and deletes backups older than 14 days. Copy that folder off the server regularly.

Restore:

```bash
docker compose stop app
docker run --rm -v hacktoberfest_aasaan-data:/data -v "$PWD/backups:/backups" alpine \
  sh -c 'cp /backups/aasaan-YYYY-MM-DD.db /data/aasaan.db && rm -f /data/aasaan.db-wal /data/aasaan.db-shm'
docker compose start app
```

(The volume name is `<project folder>_aasaan-data`; check with `docker volume ls`.)

### Azure Container Apps (ACA) & Multi-Container Deployments
On Azure Container Apps, the live SQLite database runs on an EmptyDir local volume (`/app/local/aasaan.db`) and backups persist to an Azure Files share (`/mnt/files/backups`).

1. **Pre-start Restore (`python -m app.restore`)**:
   - Runs as an init container before the app container starts.
   - If `/app/local/aasaan.db` already exists, it skips restore and exits 0.
   - If empty, it inspects `/mnt/files/backups`, verifies `PRAGMA integrity_check` on candidate backups (newest first), atomically copies the newest valid backup to `/app/local/aasaan.db`, and purges any stale `-wal` or `-shm` sidecars.
   - If no backups exist (initial deployment), it starts clean and exits 0.

2. **Continuous Online Backup (`python -m app.backup_loop`)**:
   - Runs as a sidecar or scheduled container.
   - Executes `Connection.backup` online copies into `/mnt/files/backups/aasaan-YYYY-MM-DD-HHMM.db.tmp`, runs `PRAGMA integrity_check`, and atomically renames valid backups to `.db`.
   - Prunes backups older than `AASAAN_BACKUP_KEEP_DAYS` (default: 14 days), while unconditionally keeping the newest 3 valid backups.
   - Configurable via `AASAAN_BACKUP_DIR`, `AASAAN_BACKUP_EVERY_MIN` (default: 5), and `AASAAN_BACKUP_KEEP_DAYS` (default: 14).
   - Also exposes `run_backup_once()` for programmatic or admin endpoint triggers.

## Updating

```bash
git pull
docker compose up -d --build
```

Unfinished chapters are re-queued automatically after a restart (jobs are stored in the database).

## Operations

- Logs: `docker compose logs -f app` (lines prefixed `[aasaan]`).
- Demo "kill the primary model": `curl -X POST -H "X-Admin-Token: $TOKEN" -H 'Content-Type: application/json' -d '{"model":"gemma_31b"}' https://YOUR_DOMAIN/api/admin/kill` and `/api/admin/revive` to undo.
- Privacy: no accounts and no personal data are collected. A random learner id lives in the browser; memory (mistake labels, progress) stays in the local database. Uploaded files are stored under the data volume.
- Rate limits: set `AASAAN_NB_PER_HOUR` and `AASAAN_CHAPTER_CONCURRENCY` to match your Gemini quota (check it in AI Studio).
