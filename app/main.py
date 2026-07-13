"""
Odyssey sync API — Postgres-backed storage with version history for the
Odyssey markdown writing app.

Single-user. Auth is a bearer token (ODYSSEY_API_TOKEN). The client holds
authoritative UUIDs for groups/sheets (so offline creates keep their ids),
so writes are idempotent upserts (PUT). Every sheet save records a version;
rapid successive saves within COALESCE_WINDOW_SECONDS from the same device
coalesce into the current version instead of spamming rows.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

DATABASE_URL = os.environ["ODYSSEY_DATABASE_URL"]
API_TOKEN = os.environ["ODYSSEY_API_TOKEN"]
COALESCE_WINDOW_SECONDS = int(os.environ.get("ODYSSEY_COALESCE_WINDOW", "120"))

pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8)
    yield
    await pool.close()


app = FastAPI(title="Odyssey Sync API", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------- auth
async def require_auth(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != API_TOKEN:
        raise HTTPException(403, "invalid token")


# ------------------------------------------------------------------- models
class GroupIn(BaseModel):
    name: str = "New Group"
    position: int = 0
    parent_id: Optional[str] = None


class SheetIn(BaseModel):
    group_id: str
    body: str = ""
    position: int = 0
    device: Optional[str] = None


class GoalIn(BaseModel):
    target: int
    metric: str
    comparison: str
    deadline: Optional[datetime] = None
    # Client clock, so single-user last-writer-wins is deterministic across devices.
    updated_at: Optional[datetime] = None


def _counts(body: str) -> tuple[int, int]:
    return (len(body.split()), len(body))


# -------------------------------------------------------------------- routes
@app.get("/health")
async def health():
    async with pool.acquire() as c:
        await c.fetchval("select 1")
    return {"ok": True, "service": "odyssey-api", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/library", dependencies=[Depends(require_auth)])
async def get_library():
    async with pool.acquire() as c:
        groups = await c.fetch(
            "select id, name, position, parent_id, created_at, updated_at "
            "from groups where deleted_at is null order by position, created_at"
        )
        sheets = await c.fetch(
            "select s.id, s.group_id, s.body, s.position, s.created_at, s.updated_at, "
            "  coalesce((select max(version) from sheet_versions v where v.sheet_id = s.id), 0) as version "
            "from sheets s where s.deleted_at is null order by s.position, s.updated_at desc"
        )
    by_group: dict[str, list] = {}
    for s in sheets:
        by_group.setdefault(str(s["group_id"]), []).append({
            "id": str(s["id"]),
            "group_id": str(s["group_id"]),
            "body": s["body"],
            "position": s["position"],
            "version": s["version"],
            "created_at": s["created_at"].isoformat(),
            "updated_at": s["updated_at"].isoformat(),
        })
    return {"groups": [{
        "id": str(g["id"]),
        "name": g["name"],
        "position": g["position"],
        "parent_id": str(g["parent_id"]) if g["parent_id"] else None,
        "created_at": g["created_at"].isoformat(),
        "updated_at": g["updated_at"].isoformat(),
        "sheets": by_group.get(str(g["id"]), []),
    } for g in groups]}


@app.put("/v1/groups/{group_id}", dependencies=[Depends(require_auth)])
async def upsert_group(group_id: str, payload: GroupIn):
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "insert into groups (id, name, position, parent_id) values ($1, $2, $3, $4) "
            "on conflict (id) do update set name = excluded.name, "
            "  position = excluded.position, parent_id = excluded.parent_id, "
            "  updated_at = now(), deleted_at = null "
            "returning id, name, position, parent_id, created_at, updated_at",
            group_id, payload.name, payload.position, payload.parent_id,
        )
    return dict(id=str(row["id"]), name=row["name"], position=row["position"],
                parent_id=str(row["parent_id"]) if row["parent_id"] else None,
                created_at=row["created_at"].isoformat(), updated_at=row["updated_at"].isoformat())


@app.delete("/v1/groups/{group_id}", dependencies=[Depends(require_auth)])
async def delete_group(group_id: str):
    async with pool.acquire() as c:
        await c.execute("update groups set deleted_at = now() where id = $1", group_id)
        await c.execute("update sheets set deleted_at = now() where group_id = $1", group_id)
    return {"ok": True}


@app.put("/v1/sheets/{sheet_id}", dependencies=[Depends(require_auth)])
async def upsert_sheet(sheet_id: str, payload: SheetIn):
    words, chars = _counts(payload.body)
    async with pool.acquire() as c:
        async with c.transaction():
            existing = await c.fetchrow("select body from sheets where id = $1", sheet_id)
            await c.execute(
                "insert into sheets (id, group_id, body, position) values ($1,$2,$3,$4) "
                "on conflict (id) do update set group_id = excluded.group_id, "
                "  body = excluded.body, position = excluded.position, "
                "  updated_at = now(), deleted_at = null",
                sheet_id, payload.group_id, payload.body, payload.position,
            )
            # only version when body actually changed (or first write)
            changed = existing is None or existing["body"] != payload.body
            version = None
            if changed:
                latest = await c.fetchrow(
                    "select id, version, device, updated_at from sheet_versions "
                    "where sheet_id = $1 order by version desc limit 1", sheet_id)
                now = datetime.now(timezone.utc)
                if (latest is not None
                        and latest["device"] == payload.device
                        and (now - latest["updated_at"]).total_seconds() < COALESCE_WINDOW_SECONDS):
                    # coalesce into current version
                    await c.execute(
                        "update sheet_versions set body = $1, word_count = $2, char_count = $3, "
                        "updated_at = now() where id = $4",
                        payload.body, words, chars, latest["id"])
                    version = latest["version"]
                else:
                    version = (latest["version"] + 1) if latest else 1
                    await c.execute(
                        "insert into sheet_versions (sheet_id, version, body, word_count, char_count, device) "
                        "values ($1,$2,$3,$4,$5,$6)",
                        sheet_id, version, payload.body, words, chars, payload.device)
            else:
                version = await c.fetchval(
                    "select max(version) from sheet_versions where sheet_id = $1", sheet_id) or 0
    return {"id": sheet_id, "group_id": payload.group_id, "version": version,
            "word_count": words, "char_count": chars}


@app.delete("/v1/sheets/{sheet_id}", dependencies=[Depends(require_auth)])
async def delete_sheet(sheet_id: str):
    async with pool.acquire() as c:
        await c.execute("update sheets set deleted_at = now() where id = $1", sheet_id)
    return {"ok": True}


@app.get("/v1/sheets/{sheet_id}/versions", dependencies=[Depends(require_auth)])
async def list_versions(sheet_id: str):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select version, word_count, char_count, device, created_at, updated_at "
            "from sheet_versions where sheet_id = $1 order by version desc", sheet_id)
    return {"sheet_id": sheet_id, "versions": [{
        "version": r["version"], "word_count": r["word_count"], "char_count": r["char_count"],
        "device": r["device"], "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    } for r in rows]}


@app.get("/v1/sheets/{sheet_id}/versions/{version}", dependencies=[Depends(require_auth)])
async def get_version(sheet_id: str, version: int):
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "select version, body, word_count, char_count, created_at, updated_at "
            "from sheet_versions where sheet_id = $1 and version = $2", sheet_id, version)
    if not r:
        raise HTTPException(404, "version not found")
    return {"sheet_id": sheet_id, "version": r["version"], "body": r["body"],
            "word_count": r["word_count"], "char_count": r["char_count"],
            "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()}


@app.post("/v1/sheets/{sheet_id}/restore/{version}", dependencies=[Depends(require_auth)])
async def restore_version(sheet_id: str, version: int, device: Optional[str] = None):
    async with pool.acquire() as c:
        async with c.transaction():
            src = await c.fetchrow(
                "select body from sheet_versions where sheet_id = $1 and version = $2", sheet_id, version)
            if not src:
                raise HTTPException(404, "version not found")
            words, chars = _counts(src["body"])
            await c.execute("update sheets set body = $1, updated_at = now() where id = $2",
                            src["body"], sheet_id)
            newv = (await c.fetchval("select max(version) from sheet_versions where sheet_id = $1", sheet_id)) + 1
            await c.execute(
                "insert into sheet_versions (sheet_id, version, body, word_count, char_count, device) "
                "values ($1,$2,$3,$4,$5,$6)",
                sheet_id, newv, src["body"], words, chars, device or "restore")
    return {"sheet_id": sheet_id, "restored_from": version, "new_version": newv}


# ----------------------------------------------------------------- goals sync
@app.get("/v1/goals", dependencies=[Depends(require_auth)])
async def get_goals():
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select target_id, target, metric, comparison, deadline, updated_at "
            "from goals where deleted_at is null")
    return {"goals": [{
        "target_id": str(r["target_id"]),
        "target": r["target"],
        "metric": r["metric"],
        "comparison": r["comparison"],
        "deadline": r["deadline"].isoformat() if r["deadline"] else None,
        "updated_at": r["updated_at"].isoformat(),
    } for r in rows]}


@app.put("/v1/goals/{target_id}", dependencies=[Depends(require_auth)])
async def upsert_goal(target_id: str, payload: GoalIn):
    updated = payload.updated_at or datetime.now(timezone.utc)
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "insert into goals (target_id, target, metric, comparison, deadline, updated_at, deleted_at) "
            "values ($1,$2,$3,$4,$5,$6,null) "
            "on conflict (target_id) do update set target = excluded.target, "
            "  metric = excluded.metric, comparison = excluded.comparison, "
            "  deadline = excluded.deadline, updated_at = excluded.updated_at, deleted_at = null "
            "returning target_id, target, metric, comparison, deadline, updated_at",
            target_id, payload.target, payload.metric, payload.comparison, payload.deadline, updated)
    return {
        "target_id": str(row["target_id"]),
        "target": row["target"],
        "metric": row["metric"],
        "comparison": row["comparison"],
        "deadline": row["deadline"].isoformat() if row["deadline"] else None,
        "updated_at": row["updated_at"].isoformat(),
    }


@app.delete("/v1/goals/{target_id}", dependencies=[Depends(require_auth)])
async def delete_goal(target_id: str):
    async with pool.acquire() as c:
        await c.execute("update goals set deleted_at = now() where target_id = $1", target_id)
    return {"ok": True}
