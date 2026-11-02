import asyncio
import os
import subprocess
import traceback

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db
from .agents import kill_tree, parse_status, run_turn, strip_status

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
PORT = int(os.getenv("PORT", "8787"))
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "15"))
DEFAULT_WORKSPACE = os.getenv("DEFAULT_WORKSPACE", "")
INDEX = os.path.join(ROOT, "frontend", "index.html")

app = FastAPI()
sockets: set[WebSocket] = set()
# ponytail: in-memory per-run state; runs left 'running' by a server restart just need Resume
want_loop: dict[int, bool] = {}
drivers: dict[int, asyncio.Task] = {}
procs: dict[int, set] = {}
busy_threads: set[int] = set()


async def broadcast(**event):
    for ws in list(sockets):
        try:
            await ws.send_json(event)
        except Exception:
            sockets.discard(ws)


async def set_run(run_id, **fields):
    run = db.update_run(run_id, **fields)
    await broadcast(t="run", run=run)
    return run


async def post(run_id, thread_id, role, content, meta=None):
    msg = db.add_message(run_id, thread_id, role, content, meta)
    await broadcast(t="msg", msg=msg)
    return msg


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (p.stdout + p.stderr).strip(), p.returncode


async def agent_turn(run_id, thread_id, role, prompt, resume=None, fork=False):
    """One claude turn with live streaming to the UI."""
    run = db.get_run(run_id)

    async def on_text(text):
        await broadcast(t="live", run_id=run_id, thread_id=thread_id, role=role, text=text)

    async def on_tool(summary):
        await broadcast(t="live_tool", run_id=run_id, thread_id=thread_id, role=role, tool=summary)

    def on_proc(p):
        procs.setdefault(run_id, set()).add(p)

    await broadcast(t="typing", run_id=run_id, thread_id=thread_id, role=role, on=True)
    try:
        return await run_turn(role, prompt, run["workspace"], resume=resume, fork=fork,
                              on_text=on_text, on_tool=on_tool, on_proc=on_proc)
    finally:
        procs[run_id] = {p for p in procs.get(run_id, ()) if p.returncode is None}
        await broadcast(t="typing", run_id=run_id, thread_id=thread_id, role=role, on=False)


def pending_for_planner(run_id, thread_id):
    """Main-thread messages the planner hasn't seen yet: everything to it since its last reply."""
    last = db.one("SELECT max(id) AS id FROM messages WHERE thread_id=? AND role='planner'", thread_id)["id"] or 0
    msgs = db.all_("SELECT * FROM messages WHERE thread_id=? AND id>? ORDER BY id", thread_id, last)
    return [m for m in msgs if m["role"] in ("aditya", "executor") or m["meta"].get("to_planner")]


def planner_prompt(msgs):
    parts = []
    for m in msgs:
        if m["role"] == "executor":
            parts.append(f"Executor report (round {m['meta'].get('round')}):\n{m['content']}\n\n"
                         f"git diff --stat HEAD~1:\n{m['meta'].get('diffstat') or '(no changes)'}")
        elif m["role"] == "aditya":
            parts.append(f"Aditya: {m['content']}")
        elif m["meta"].get("to_planner"):
            parts.append(f"[System] {m['content']}")
    return "\n\n---\n\n".join(parts)


async def planner_step(run_id, thread, pending):
    run = db.get_run(run_id)
    res = await agent_turn(run_id, thread["id"], "planner", planner_prompt(pending), resume=run["planner_session"])
    if res["session_id"]:
        await set_run(run_id, planner_session=res["session_id"])
    status = "ERROR" if res["is_error"] else parse_status(res["text"])
    await post(run_id, thread["id"], "planner", res["text"], {"status": status, "tools": res["tools"]})
    return status


async def executor_step(run_id, thread, instruction):
    run = await set_run(run_id, round=db.get_run(run_id)["round"] + 1)
    n, ws = run["round"], run["workspace"]
    res = await agent_turn(run_id, thread["id"], "executor", instruction, resume=run["executor_session"])
    if res["session_id"]:
        await set_run(run_id, executor_session=res["session_id"])
    git(ws, "add", "-A")
    out, code = git(ws, "commit", "--allow-empty", "-m", f"sgc: round {n}")
    diffstat = git(ws, "diff", "--stat", "HEAD~1")[0] if code == 0 else f"commit failed: {out}"
    await post(run_id, thread["id"], "executor", res["text"],
               {"round": n, "diffstat": diffstat, "tools": res["tools"], "is_error": res["is_error"]})


async def drive(run_id):
    """The single driver per run. Loops while want_loop; otherwise only answers Aditya's pending messages."""
    thread = db.main_thread(run_id)
    try:
        while True:
            looping = want_loop.get(run_id)
            pending = pending_for_planner(run_id, thread["id"])
            # paused with only an executor report waiting: hold the review until Resume
            if pending and (looping or any(m["role"] != "executor" for m in pending)):
                status = await planner_step(run_id, thread, pending)
                if status == "ERROR":
                    want_loop[run_id] = False
                    await post(run_id, thread["id"], "system", "Planner turn failed; loop paused.")
                    if looping:
                        await set_run(run_id, status="paused")
                continue
            if not looping:
                break
            last = db.one("SELECT * FROM messages WHERE thread_id=? AND role='planner' ORDER BY id DESC LIMIT 1",
                          thread["id"])
            status = last["meta"].get("status") if last else "NEED_ADITYA"
            if status != "CONTINUE":
                want_loop[run_id] = False
                if status == "DONE":
                    await post(run_id, thread["id"], "system", "Planner marked the work DONE.")
                    await set_run(run_id, status="done")
                else:
                    await post(run_id, thread["id"], "system", "Planner needs Aditya. Reply, then Resume.")
                    await set_run(run_id, status="paused")
                break
            if db.get_run(run_id)["round"] >= MAX_ROUNDS:
                want_loop[run_id] = False
                await post(run_id, thread["id"], "system", f"Round cap ({MAX_ROUNDS}) reached; loop stopped.")
                await set_run(run_id, status="stopped")
                break
            await executor_step(run_id, thread, strip_status(last["content"]))
            if not want_loop.get(run_id):
                await post(run_id, thread["id"], "system", "Paused after executor step.")
    except Exception as e:
        want_loop[run_id] = False
        await post(run_id, thread["id"], "system", f"Driver error: {e!r}")
        await set_run(run_id, status="paused")
        traceback.print_exc()
    finally:
        if drivers.get(run_id) is asyncio.current_task():
            drivers.pop(run_id)
        if db.get_run(run_id)["status"] == "running":  # paused mid-step
            await set_run(run_id, status="paused")


def kick(run_id):
    if run_id not in drivers:
        drivers[run_id] = asyncio.create_task(drive(run_id))


# ---------- API ----------

class NewRun(BaseModel):
    workspace: str


class Msg(BaseModel):
    text: str
    thread_id: int | None = None


class Branch(BaseModel):
    message_id: int


class Close(BaseModel):
    note: str = ""


def need_run(run_id):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return run


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/config")
def config():
    return {"max_rounds": MAX_ROUNDS, "default_workspace": DEFAULT_WORKSPACE}


@app.get("/api/runs")
def list_runs():
    return db.all_("SELECT * FROM runs ORDER BY id DESC")


@app.post("/api/runs")
def create_run(body: NewRun):
    ws = os.path.abspath(body.workspace.strip().strip('"'))
    os.makedirs(ws, exist_ok=True)
    if git(ws, "rev-parse", "--is-inside-work-tree")[1] != 0:
        git(ws, "init")
    if git(ws, "rev-parse", "HEAD")[1] != 0:  # HEAD~1 diffs need a base commit
        git(ws, "add", "-A")
        out, code = git(ws, "commit", "--allow-empty", "-m", "sgc: initial")
        if code:
            raise HTTPException(400, f"git commit failed in workspace: {out}")
    run_id = db.insert("INSERT INTO runs(workspace, created_at) VALUES(?,?)", ws, db.now())
    db.insert("INSERT INTO threads(run_id, kind) VALUES(?, 'main')", run_id)
    return db.get_run(run_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = need_run(run_id)
    return {"run": run, "threads": db.all_("SELECT * FROM threads WHERE run_id=?", run_id),
            "messages": db.all_("SELECT * FROM messages WHERE run_id=? ORDER BY id", run_id),
            "busy": run_id in drivers, "looping": bool(want_loop.get(run_id))}


@app.post("/api/runs/{run_id}/message")
async def message(run_id: int, body: Msg):
    need_run(run_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty message")
    thread = db.one("SELECT * FROM threads WHERE id=? AND run_id=?", body.thread_id, run_id) if body.thread_id \
        else db.main_thread(run_id)
    if not thread or thread["status"] != "open":
        raise HTTPException(400, "thread not open")
    if thread["kind"] == "main":
        await post(run_id, thread["id"], "aditya", text)
        kick(run_id)  # a running driver picks it up as pending context
        return {"ok": True}
    if thread["id"] in busy_threads:
        raise HTTPException(409, "branch is still answering")
    busy_threads.add(thread["id"])
    await post(run_id, thread["id"], "aditya", text)
    asyncio.create_task(branch_turn(run_id, thread, text))
    return {"ok": True}


async def branch_turn(run_id, thread, text):
    try:
        prompt, resume, fork = f"Aditya: {text}", thread["planner_session"], False
        if not resume:  # first turn: fork the main planner so the side thread doesn't pollute it
            parent = db.one("SELECT * FROM messages WHERE id=?", thread["parent_message_id"])
            resume, fork = db.get_run(run_id)["planner_session"], True
            prompt = (f"[Side thread. Aditya clicked this {parent['role']} message to ask about it. This is a "
                      f"side conversation: answer him, do not instruct the Executor. Still end with a STATUS "
                      f"line.]\n\n> {parent['content'][:4000]}\n\n{prompt}")
        res = await agent_turn(run_id, thread["id"], "planner", prompt, resume=resume, fork=fork)
        if res["session_id"]:
            db.conn.execute("UPDATE threads SET planner_session=? WHERE id=?", (res["session_id"], thread["id"]))
        await post(run_id, thread["id"], "planner", res["text"], {"tools": res["tools"]})
    finally:
        busy_threads.discard(thread["id"])


@app.post("/api/runs/{run_id}/branches")
async def branch(run_id: int, body: Branch):
    need_run(run_id)
    if not db.one("SELECT id FROM messages WHERE id=? AND run_id=?", body.message_id, run_id):
        raise HTTPException(404, "no such message")
    tid = db.insert("INSERT INTO threads(run_id, kind, parent_message_id) VALUES(?, 'branch', ?)",
                    run_id, body.message_id)
    thread = db.one("SELECT * FROM threads WHERE id=?", tid)
    await broadcast(t="thread", thread=thread)
    return thread


@app.post("/api/threads/{thread_id}/close")
async def close_thread(thread_id: int, body: Close):
    thread = db.one("SELECT * FROM threads WHERE id=? AND kind='branch'", thread_id)
    if not thread:
        raise HTTPException(404, "no such branch")
    db.conn.execute("UPDATE threads SET status='closed' WHERE id=?", (thread_id,))
    await broadcast(t="thread", thread={**thread, "status": "closed"})
    if body.note.strip():
        await message(thread["run_id"], Msg(text=f"(note from side thread) {body.note.strip()}"))
    return {"ok": True}


@app.post("/api/runs/{run_id}/{action}")
async def control(run_id: int, action: str):
    run = need_run(run_id)
    main = db.main_thread(run_id)
    if action in ("start", "resume"):
        if want_loop.get(run_id):
            raise HTTPException(409, "already running")
        if action == "start":
            await post(run_id, main["id"], "system",
                       "Aditya pressed Start. Write the first instruction for the Executor now.", {"to_planner": True})
        want_loop[run_id] = True
        await set_run(run_id, status="running")
        kick(run_id)
    elif action == "pause":
        if run["status"] != "running":
            raise HTTPException(409, "not running")
        want_loop[run_id] = False
        await post(run_id, main["id"], "system", "Pause requested; stopping after the current step.")
    elif action == "stop":
        want_loop[run_id] = False
        for p in procs.pop(run_id, set()):
            if p.returncode is None:
                kill_tree(p.pid)
        task = drivers.pop(run_id, None)
        if task:
            task.cancel()
        await post(run_id, main["id"], "system", "Stopped by Aditya.")
        await set_run(run_id, status="stopped")
    else:
        raise HTTPException(404, "unknown action")
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()  # client doesn't send anything meaningful; this just detects disconnect
    except WebSocketDisconnect:
        sockets.discard(ws)


if __name__ == "__main__":
    # No reload: reload mode can swap in the selector loop, which can't spawn subprocesses on Windows.
    uvicorn.run(app, host="127.0.0.1", port=PORT)
