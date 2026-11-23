# Running with Docker (Phase 15)

One command starts the database, the API and the dashboard, on any computer that has Docker. Nothing else has to be installed: no Python, no Node.js, no MongoDB.

```powershell
docker compose up --build
```
Open **http://127.0.0.1:8000** (dashboard) or **http://127.0.0.1:8000/docs** (API documentation). Stop with Ctrl+C; `docker compose up -d` runs it in the background instead.
The first build downloads about 2 GB (the MongoDB image is 1.3 GB, the app image is about 860 MB) and takes a few minutes; later starts take seconds.

## What runs

| Container | Image | Role |
|---|---|---|
| `mongo` | `mongo:8.2` | the database. **Not published to the host**: only the app, on the private compose network, can reach it. Its cache is capped (`MONGO_CACHE_GB`, default 2) so it does not take half of your memory |
| `app` | built from `docker/Dockerfile` | the API **and** the dashboard at one address (the same design as [RUNNING.md](RUNNING.md)); runs as an ordinary user (uid 10001), never root |

On every start the app container runs `scripts/init_db.py` (collections, validators, indexes, seed instruments). It is idempotent and never deletes data. `docker compose` waits for MongoDB to be
healthy before starting the app, and the app's own health check calls `/api/health`, which answers 503 when the database is unreachable, so **"healthy" means the API and the database both work**
(`docker compose ps`).

The image is built in two stages: Node.js builds the dashboard, and only the finished files are copied into a slim Python image. Node.js, the tests and the docs are not in the final image.

## Settings

Set them as shell variables or in a `.env` file next to `docker-compose.yml` (Compose reads it automatically).

| Setting | Default | Meaning |
|---|---|---|
| `APP_PORT` | `8000` | port on this computer. If it is taken you get "ports are not available": pick another, e.g. `$env:APP_PORT=8765; docker compose up -d` |
| `ENVIRONMENT` | `development` | `production` disables every data-changing endpoint unless `API_KEY` is set |
| `API_KEY` | none | the `X-API-Key` that data-changing endpoints (import, processing, backtests) require |
| `MONGO_CACHE_GB` | `2` | MongoDB's cache size |
| `WITH_SPARK` | `0` | build argument: `1` adds Java 21 and PySpark (about 1.3 GB more, 2.1 GB in total) for `scripts/bigdata.py` |

The container's `.env` is never copied in: `.dockerignore` keeps `.env`, `kaggle.json`, `.git`, your data and your logs out of the image, and the compose file contains no secret. Put real values in your shell or `.env`.
The container does **not** read your normal `.env` (that file points at `localhost`, which inside a container is the container itself); the compose file sets `MONGO_URI=mongodb://mongo:27017` for it.

## Getting data into the container

The Docker database starts with the 16 seed instruments and **no prices**: it is a separate database from the MongoDB installed on your computer. Two ways to fill it.

### A. Import files (the normal way)
`./data` on your computer is mounted at `/app/data` in the container, so anything you put under `data\raw` or `data\uploads` is visible to the importer. Use paths **relative to `/app`**
(the container's working directory), the same as on Windows without the `.venv\Scripts\python` prefix:

```powershell
docker compose exec app python scripts/import_data.py import data/raw/yfinance data/raw/kaggle --auto-register
docker compose exec app python scripts/process_data.py all
docker compose exec app python scripts/analyze_data.py all
docker compose exec app python scripts/backtest.py compare        # optional
docker compose exec app python scripts/import_data.py summary
```
A file imported without its `.meta.json` sidecar needs `--interval 1d` (or whatever its interval is). Downloads work too: `docker compose exec app python scripts/download_data.py yfinance --interval 1d`
(the files land in your `data\` folder). The dashboard's **Data & imports** page uploads to the same place.

### B. Copy the database you already have
This moves everything (prices, adjusted prices, indicators, analytics, backtests) from MongoDB on your computer into the container without re-running anything. The MongoDB container
already contains `mongodump` and `mongorestore`, and `host.docker.internal` is how a container reaches your computer:

```powershell
docker compose up -d
docker compose exec mongo sh -c "mongodump --host host.docker.internal --port 27017 --db BharatMarketDB --archive | mongorestore --archive --drop"
```
`--drop` replaces each collection in the **container's** database; your local database is only read. Expect minutes and several GB for the full 5.76 million rows plus indicators.
If it cannot connect, use method A instead. The connection and the dump-and-restore were tested with the `instruments` collection (56 documents from the local database arrived intact); the
full-size copy was not run.

Either way the data lives in the `mongo-data` volume (below), so it survives restarts and rebuilds.

## Where things are stored

| What | Where | Survives `docker compose down` | Removed by `docker compose down -v` |
|---|---|---|---|
| The database | volume `bharatmarket_mongo-data` | yes | **yes: all of it** |
| Application logs | volume `bharatmarket_app-logs` | yes | yes |
| Downloaded, imported and uploaded files | `data\` on your computer | yes | no |

Rebuilding (`docker compose up --build`) never touches the volumes. Only `-v` (or `docker volume rm`) deletes the database, so never add `-v` to a command you copy from somewhere else unless you mean it.

To back up, copy the volume's contents out with `mongodump` inside the container and then out of it:
```powershell
docker compose exec mongo mongodump --db BharatMarketDB --archive=/data/db/backup.archive
docker compose cp mongo:/data/db/backup.archive .\bharatmarket-backup.archive
```
To restore, copy it back with `docker compose cp` and run `mongorestore --archive=/data/db/backup.archive --drop` the same way. (Do not redirect `mongodump` output with `>` in Windows
PowerShell 5: it re-encodes binary data and corrupts the archive.)

## Everyday commands

| Goal | Command |
|---|---|
| Start in the background | `docker compose up -d` |
| Is it healthy? | `docker compose ps` |
| Watch the logs | `docker compose logs -f app` |
| Stop (keeps everything) | `docker compose down` |
| Rebuild after changing the code | `docker compose up --build -d` |
| A shell in the app container | `docker compose exec app sh` |
| A MongoDB shell | `docker compose exec mongo mongosh BharatMarketDB` |
| Add Spark | `docker compose build --build-arg WITH_SPARK=1`, then `docker compose up -d` and `docker compose exec app python scripts/bigdata.py check` |
| Remove everything including the database | `docker compose down -v` |

## Security

- The app is published on **127.0.0.1 only**: reachable from this computer, not from your network. To share it, change the port line in `docker-compose.yml` to `"${APP_PORT:-8000}:8000"`, **and first** read
  [RUNNING.md](RUNNING.md#exposing-it-beyond-your-machine): reading data has no login, so set `API_KEY`, set `ENVIRONMENT=production`, and put authentication or a firewall and HTTPS in front.
- MongoDB has no username or password, which is acceptable **only because it is not published**. Never add a `ports:` entry to the `mongo` service. A test (`tests/test_docker_files.py`) fails if someone does.
- Inside the container the app sees every client as the Docker gateway address (for example `172.19.0.1`), so the per-address rate limit is shared by everyone who reaches it through the published port.
  That is fine for one user on one computer; put a reverse proxy in front before serving several people.
- The container runs as an unprivileged user and holds no secrets other than the ones you pass in.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ports are not available` / `bind: Only one usage of each socket address` | the port is taken (another server, or Windows reserved it): `$env:APP_PORT=8765; docker compose up -d` |
| `Cannot connect to the Docker daemon` / `error during connect` | start Docker Desktop and wait until it says it is running |
| The dashboard shows "Market analytics have not been computed yet" and 0 prices | the container's database is new and empty: see *Getting data into the container* |
| `No importable files found` after `exec ... import /app/data/...` in Git Bash | Git Bash rewrites paths that start with `/`; use relative paths (`data/raw/...`) or PowerShell |
| `unsupported interval None` | the file has no `.meta.json`: add `--interval 1d` |
| The app container restarts or is `unhealthy` | `docker compose logs app`: usually MongoDB is not healthy yet, or `MONGO_URI` was overridden |
| `exec /entrypoint.sh: no such file or directory` | the script has Windows line endings (`CRLF`); `.gitattributes` forces LF and the Dockerfile strips CR, so this means one of those was removed |
| Changed the code but the app is the same | the code is baked into the image: `docker compose up --build -d` |
| MongoDB uses too much memory | lower `MONGO_CACHE_GB`; also see Docker Desktop's memory limit (Settings, Resources) |
| Build cannot download packages | check the network or proxy; the first build needs the internet |
| `WITH_SPARK=1` fails at `apt-get` | the base image (`python:3.11-slim`) moved to a Debian release without that Java package: change the package in `docker/Dockerfile` to one `apt-cache search openjdk` lists |

## What was verified (Docker 29, Compose 5, Windows)

`docker compose build` and `up` succeed; both containers become healthy; the dashboard, a deep link (`/stock?symbol=TCS`), the API documentation and a JSON 404 for an unknown API path
respond with the same security headers as the non-Docker setup; the process runs as uid 10001; MongoDB has no published port; a real CSV (6,990 rows) imported inside the container in 0.6 s,
the processing step produced adjusted prices that the API then served, and the dashboard showed them in a browser; the data survived `docker compose down` followed by `up`; and a dump from
the local MongoDB restored into the container.
Found and fixed while verifying: the Spark option installed Java 17, which the Debian 13 base image no longer has (it failed with exit code 100); it now installs Java 21, which PySpark 4 supports. With that, `scripts/bigdata.py check` in the Spark image starts Spark 4.3 and sums 10 million numbers in parallel.

The guards in `tests/test_docker_files.py` (package list in step with `requirements.txt`, nothing copied that does not exist, LF line endings, no secrets, database not published) run with the normal
test suite and do not need Docker.
