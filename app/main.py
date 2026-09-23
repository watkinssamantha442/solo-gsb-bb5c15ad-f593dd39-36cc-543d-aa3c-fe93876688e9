"""FastAPI application: script CRUD, validation, sharing, runtime preview runs."""
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import get_db, init_db
from .graph import step as graph_step
from .graph import validate as validate_content

app = FastAPI(title="分支访谈脚本编辑器", version="1.0.0")

STATIC_DIR = "static"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- models ---------------------------------------------------------------

class OptionIn(BaseModel):
    id: str
    label: str = ""
    target_id: str | None = None


class NodeIn(BaseModel):
    id: str
    type: str = "question"          # question | info
    title: str = ""
    text: str = ""
    terminal: bool = False
    x: float = 0
    y: float = 0
    options: list[OptionIn] = Field(default_factory=list)


class ContentIn(BaseModel):
    start_node_id: str | None = None
    nodes: list[NodeIn] = Field(default_factory=list)


class ScriptIn(BaseModel):
    name: str = "未命名访谈脚本"
    content: ContentIn | None = None


class ScriptUpdate(BaseModel):
    name: str | None = None
    content: ContentIn | None = None


class RunStart(BaseModel):
    script_id: int | None = None
    share_token: str | None = None


class RunChoice(BaseModel):
    option_id: str


# ---- lifecycle ------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    init_db()


# ---- script CRUD ----------------------------------------------------------

@app.get("/api/scripts")
def list_scripts():
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created_at, updated_at FROM scripts ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/scripts", status_code=201)
def create_script(payload: ScriptIn):
    content = payload.content.model_dump() if payload.content else {"start_node_id": None, "nodes": []}
    ts = now_iso()
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO scripts (name, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (payload.name, json.dumps(content, ensure_ascii=False), ts, ts),
        )
        sid = cur.lastrowid
    return {"id": sid, "name": payload.name, "content": content,
            "created_at": ts, "updated_at": ts}


def _fetch_script(script_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "脚本不存在")
    return {"id": row["id"], "name": row["name"], "content": json.loads(row["content"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@app.get("/api/scripts/{script_id}")
def get_script(script_id: int):
    return _fetch_script(script_id)


@app.put("/api/scripts/{script_id}")
def update_script(script_id: int, payload: ScriptUpdate):
    existing = _fetch_script(script_id)
    name = payload.name if payload.name is not None else existing["name"]
    content = payload.content.model_dump() if payload.content is not None else existing["content"]
    ts = now_iso()
    with get_db() as db:
        db.execute(
            "UPDATE scripts SET name = ?, content = ?, updated_at = ? WHERE id = ?",
            (name, json.dumps(content, ensure_ascii=False), ts, script_id),
        )
    return {"id": script_id, "name": name, "content": content, "updated_at": ts}


@app.delete("/api/scripts/{script_id}", status_code=204)
def delete_script(script_id: int):
    _fetch_script(script_id)
    with get_db() as db:
        db.execute("DELETE FROM scripts WHERE id = ?", (script_id,))


# ---- validation -----------------------------------------------------------

@app.post("/api/scripts/{script_id}/validate")
def validate_saved_script(script_id: int):
    script = _fetch_script(script_id)
    result = validate_content(script["content"])
    return result


# ---- import / export ------------------------------------------------------

@app.get("/api/scripts/{script_id}/export")
def export_script(script_id: int):
    script = _fetch_script(script_id)
    return {
        "format": "branch-interview-script",
        "version": 1,
        "name": script["name"],
        "content": script["content"],
        "exported_at": now_iso(),
    }


@app.post("/api/import")
def import_script(payload: dict[str, Any]):
    """Import a JSON document previously produced by /export (or a raw content object)."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON 顶层必须是对象")
    if "content" in payload:
        content, name = payload["content"], payload.get("name", "导入的访谈脚本")
    else:
        content, name = payload, payload.get("name", "导入的访谈脚本")
        # Raw content documents only carry start_node_id / nodes.
        if "start_node_id" not in content or "nodes" not in content:
            raise HTTPException(400, "缺少 content 或 start_node_id/nodes 字段")
    try:
        ContentIn.model_validate(content)
    except Exception as exc:
        raise HTTPException(400, f"脚本格式不正确: {exc}")
    ts = now_iso()
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO scripts (name, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (str(name), json.dumps(content, ensure_ascii=False), ts, ts),
        )
        sid = cur.lastrowid
    return {"id": sid, "name": name, "content": content, "created_at": ts}


# ---- read-only sharing ----------------------------------------------------

@app.post("/api/scripts/{script_id}/share")
def create_share(script_id: int):
    script = _fetch_script(script_id)
    token = secrets.token_urlsafe(8)
    ts = now_iso()
    with get_db() as db:
        db.execute(
            "INSERT INTO shares (token, script_id, script_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, script_id, script["name"],
             json.dumps(script["content"], ensure_ascii=False), ts),
        )
    return {"token": token, "url": f"/share.html?token={token}"}


@app.get("/api/share/{token}")
def get_share(token: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise HTTPException(404, "分享链接不存在或已失效")
    return {"token": row["token"], "name": row["script_name"],
            "content": json.loads(row["content"]), "created_at": row["created_at"],
            "read_only": True}


# ---- runtime preview ------------------------------------------------------

def _run_content(db, row) -> dict[str, Any]:
    if row["mode"] == "share":
        share = db.execute("SELECT content FROM shares WHERE token = ?",
                           (row["share_token"],)).fetchone()
        if share is None:
            raise HTTPException(404, "分享不存在")
        return json.loads(share["content"])
    script = db.execute("SELECT content FROM scripts WHERE id = ?",
                        (row["script_id"],)).fetchone()
    if script is None:
        raise HTTPException(404, "脚本不存在")
    return json.loads(script["content"])


@app.post("/api/runs", status_code=201)
def start_run(payload: RunStart):
    if not payload.script_id and not payload.share_token:
        raise HTTPException(400, "需要 script_id 或 share_token")
    with get_db() as db:
        content = _run_content_for(db, payload)
        try:
            result = graph_step(content, "", None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        node = result["node"]
        steps = [{"node_id": node["id"], "title": node.get("title", ""),
                  "question": node.get("text", ""), "option_id": None, "answer": None}]
        finished = bool(node.get("terminal")) or not node.get("options")
        cur = db.execute(
            "INSERT INTO runs (mode, script_id, share_token, current_node_id, steps, finished, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("share" if payload.share_token else "script", payload.script_id,
             payload.share_token, node["id"], json.dumps(steps, ensure_ascii=False),
             int(finished), now_iso()),
        )
        run_id = cur.lastrowid
    return {"run_id": run_id, "current_node": node, "steps": steps, "finished": finished}


def _run_content_for(db, payload: RunStart) -> dict[str, Any]:
    if payload.share_token:
        row = db.execute("SELECT content FROM shares WHERE token = ?",
                         (payload.share_token,)).fetchone()
        if row is None:
            raise HTTPException(404, "分享不存在")
        return json.loads(row["content"])
    row = db.execute("SELECT content FROM scripts WHERE id = ?",
                     (payload.script_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "脚本不存在")
    return json.loads(row["content"])


def _load_run(db, run_id: int):
    row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "预览会话不存在")
    return row


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    with get_db() as db:
        row = _load_run(db, run_id)
        content = _run_content(db, row)
        node = next((n for n in content["nodes"] if n["id"] == row["current_node_id"]), None)
        steps = json.loads(row["steps"])
    return {"run_id": run_id, "current_node": node, "steps": steps,
            "finished": bool(row["finished"])}


@app.post("/api/runs/{run_id}/choice")
def choose(run_id: int, payload: RunChoice):
    with get_db() as db:
        row = _load_run(db, run_id)
        if row["finished"]:
            raise HTTPException(400, "访谈已结束，请重新开始")
        content = _run_content(db, row)
        try:
            result = graph_step(content, row["current_node_id"], payload.option_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        node = result["node"]
        steps = json.loads(row["steps"])
        current_node = next((n for n in content["nodes"] if n["id"] == row["current_node_id"]), None)
        if current_node is None:
            raise HTTPException(400, "当前节点已被编辑删除，请重新开始预览")
        opt = next((o for o in current_node.get("options", [])
                    if o["id"] == payload.option_id), None)
        steps[-1]["option_id"] = payload.option_id
        steps[-1]["answer"] = opt.get("label") if opt else payload.option_id
        steps.append({"node_id": node["id"], "title": node.get("title", ""),
                      "question": node.get("text", ""), "option_id": None, "answer": None})
        finished = bool(node.get("terminal")) or not node.get("options")
        db.execute("UPDATE runs SET current_node_id = ?, steps = ?, finished = ? WHERE id = ?",
                   (node["id"], json.dumps(steps, ensure_ascii=False), int(finished), run_id))
    return {"run_id": run_id, "current_node": node, "steps": steps, "finished": finished}


@app.post("/api/runs/{run_id}/restart")
def restart_run(run_id: int):
    with get_db() as db:
        row = _load_run(db, run_id)
        content = _run_content(db, row)
        try:
            result = graph_step(content, "", None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        node = result["node"]
        steps = [{"node_id": node["id"], "title": node.get("title", ""),
                  "question": node.get("text", ""), "option_id": None, "answer": None}]
        finished = bool(node.get("terminal")) or not node.get("options")
        db.execute("UPDATE runs SET current_node_id = ?, steps = ?, finished = ? WHERE id = ?",
                   (node["id"], json.dumps(steps, ensure_ascii=False), int(finished), run_id))
    return {"run_id": run_id, "current_node": node, "steps": steps, "finished": finished}


# ---- static frontend ------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(f"{STATIC_DIR}/index.html")


@app.get("/share.html", include_in_schema=False)
def share_page():
    return FileResponse(f"{STATIC_DIR}/share.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
