# Odyssey Sync API

Postgres-backed storage with **version history** for the [Odyssey](https://github.com/cwfrazier1/odyssey-ios)
markdown writing app. Single-user, bearer-token auth, FastAPI + asyncpg.

- **Host:** CT 227 (`odyssey-api`, 192.168.1.69) on Proxmox 192.168.1.14
- **Public:** https://odyssey.cwfrazier.com (nginx reverse proxy on CT 200 + Let's Encrypt)
- **DB:** PostgreSQL 15 on the same container, database `odyssey`

## Data model

| table | purpose |
|-------|---------|
| `groups` | Ulysses-style folders (soft-deleted) |
| `sheets` | current body of each sheet (soft-deleted) |
| `sheet_versions` | append-only history; one row per save point |

Versioning coalesces rapid saves from the same device inside
`ODYSSEY_COALESCE_WINDOW` seconds (default 120) so history is a useful
time-machine rather than a keystroke log. First save and every
cross-window edit create a new version.

## API

All routes except `/health` require `Authorization: Bearer <ODYSSEY_API_TOKEN>`.
The client owns the UUIDs (offline-created ids survive), so writes are
idempotent `PUT` upserts.

```
GET    /health
GET    /v1/library                              full state (groups + sheets)
PUT    /v1/groups/{id}        {name, position}  upsert group
DELETE /v1/groups/{id}                          soft-delete group + its sheets
PUT    /v1/sheets/{id}        {group_id, body, position, device}  upsert + version
DELETE /v1/sheets/{id}                          soft-delete sheet
GET    /v1/sheets/{id}/versions                 version metadata list
GET    /v1/sheets/{id}/versions/{version}       one version's body
POST   /v1/sheets/{id}/restore/{version}        restore (writes a new version)
```

## Deploy

```sh
# on CT 227
sudo -u postgres psql -d odyssey -f deploy/schema.sql
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in DB url + token
sudo cp deploy/odyssey-api.service /etc/systemd/system/
sudo systemctl enable --now odyssey-api
```
