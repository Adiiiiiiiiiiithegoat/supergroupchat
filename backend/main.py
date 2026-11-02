import asyncio
import os
import subprocess
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db
from .agents import check_claude_md_exclusion, kill_tree, parse_status, run_turn, strip_status

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
PORT = int(os.getenv("PORT", "8787"))
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "15"))
DEFAULT_WORKSPACE = os.getenv("DEFAULT_WORKSPACE", "")
INDEX = os.path.join(ROOT, "frontend", "index.html")
SKIP_STARTUP_CHECK = os.getenv("SKIP_STARTUP_CHECK") == "1"
AUTO_RESUME_GRACE = 60  # seconds after a usage-limit reset before auto-resuming
startup_warning = None


def reset_stale_runs():
    # Drivers and processes don't survive a restart, so 'running' would be a lie; Resume picks up from the db.
    db.conn.execute("UPDATE runs SET status='paused' WHERE status='running'")


async def startup_check():
    """claudeMdExcludes is undocumented; make sure a CLI update hasn't silently broken it."""
    global startup_warning
    try:
        warning, version = await check_claude_md_exclusion()
    except Exception as e:
        warning, version = f"CLAUDE.md self-check errored: {e!r}", "unknown"
    print(f"[sgc] CLAUDE.md exclusion self-check, Claude Code {version}: {'FAILED' if warning else 'ok'}", flush=True)
    if warning:
        startup_warning = f"{warning} (Claude Code {version})"
        print(f"[sgc] WARNING: {startup_warning}", flush=True)
        await broadcast(t="warning", text=startup_warning)


@asynccontextmanager
async def lifespan(app):
    reset_stale_runs()
    reschedule_auto_resumes()
    if SKIP_STARTUP_CHECK:
        print("[sgc] CLAUDE.md self-check skipped (SKIP_STARTUP_CHECK=1)", flush=True)
    else:
        asyncio.create_task(startup_check())  # background: costs one short turn, mustn't block the page
    yield


app = FastAPI(lifespan=lifespan)
sockets: set[WebSocket] = set()
# ponytail: in-memory per-run state; reset_stale_runs() pauses leftover 'running' runs at startup
want_loop: dict[int, bool] = {}
drivers: dict[int, asyncio.Task] = {}
procs: dict[int, set] = {}
busy_threads: set[int] = set()
auto_resumes: dict[int, asyncio.Task] = {}


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


async def post_limit(run_id, thread_id, who, res, loop=False):
    """Report a usage limit; if the loop was running and the reset time is known, schedule an auto-resume."""
    ts = res["limited"].get("resets_at")
    if ts and loop:
        at = ts + AUTO_RESUME_GRACE
        await set_run(run_id, auto_resume_at=at)
        schedule_auto_resume(run_id, at)
        tail = f" Auto-resuming at {datetime.fromtimestamp(at):%H:%M on %b %d}."
    elif ts:
        tail = f" It resets around {datetime.fromtimestamp(ts):%H:%M on %b %d}. Resume after the reset."
    else:
        tail = " Resume after the reset."
    await post(run_id, thread_id, "system",
               f"{who} hit the Claude usage limit, so the run is paused.{tail}\n\n{res['text']}", {"limit": True})


def schedule_auto_resume(run_id, at):
    if old := auto_resumes.pop(run_id, None):
        old.cancel()
    auto_resumes[run_id] = asyncio.create_task(auto_resume(run_id, at))


async def auto_resume(run_id, at):
    await asyncio.sleep(max(0, at - time.time()))
    while run_id in drivers:  # the driver that hit the limit may still be winding down
        await asyncio.sleep(0.1)
    auto_resumes.pop(run_id, None)
    run = await set_run(run_id, auto_resume_at=None)
    if run["status"] == "paused":
        await post(run_id, db.main_thread(run_id)["id"], "system", "Usage limit should have reset; auto-resuming.")
        await resume_loop(run_id)


async def cancel_auto_resume(run_id):
    if task := auto_resumes.pop(run_id, None):
        task.cancel()
    if db.get_run(run_id)["auto_resume_at"] is not None:
        await set_run(run_id, auto_resume_at=None)


def reschedule_auto_resumes():
    # scheduled tasks die with the server; the time is on the run, so put them back (overdue ones fire at once)
    for run in db.all_("SELECT id, auto_resume_at FROM runs WHERE auto_resume_at IS NOT NULL"):
        schedule_auto_resume(run["id"], run["auto_resume_at"])


async def resume_loop(run_id):
    want_loop[run_id] = True
    await set_run(run_id, status="running")
    kick(run_id)


async def planner_step(run_id, thread, pending):
    run = db.get_run(run_id)
    res = await agent_turn(run_id, thread["id"], "planner", planner_prompt(pending), resume=run["planner_session"])
    if res["session_id"]:
        await set_run(run_id, planner_session=res["session_id"])
    if res["limited"]:  # same as an error: inputs stay pending for the retry
        await post_limit(run_id, thread["id"], "Planner", res, loop=want_loop.get(run_id))
        return "ERROR"
    if res["is_error"]:
        # Not a planner message: pending_for_planner() counts from the last planner message,
        # so the executor report / Aditya's message stay pending and are re-sent on retry.
        await post(run_id, thread["id"], "system", f"Planner turn failed. Resume or send a message to retry.\n\n{res['text']}")
        return "ERROR"
    status = parse_status(res["text"])
    await post(run_id, thread["id"], "planner", res["text"], {"status": status, "tools": res["tools"]})
    return status


async def executor_step(run_id, thread, instruction):
    """Returns False if a usage limit cut it off: the round isn't counted or committed, and Resume reruns it."""
    run = db.get_run(run_id)
    n, ws = run["round"] + 1, run["workspace"]
    res = await agent_turn(run_id, thread["id"], "executor", instruction, resume=run["executor_session"])
    if res["session_id"]:
        await set_run(run_id, executor_session=res["session_id"])
    if res["limited"]:
        await post_limit(run_id, thread["id"], "Executor", res, loop=True)
        return False
    await set_run(run_id, round=n)
    git(ws, "add", "-A")
    out, code = git(ws, "commit", "--allow-empty", "-m", f"sgc: round {n}")
    diffstat = git(ws, "diff", "--stat", "HEAD~1")[0] if code == 0 else f"commit failed: {out}"
    await post(run_id, thread["id"], "executor", res["text"],
               {"round": n, "diffstat": diffstat, "tools": res["tools"], "is_error": res["is_error"]})
    return True


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
                if status == "ERROR":  # stop; the pending messages are still pending, so no hot retry loop
                    want_loop[run_id] = False
                    if looping:
                        await set_run(run_id, status="paused")
                    break
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
            if not await executor_step(run_id, thread, strip_status(last["content"])):
                want_loop[run_id] = False
                await set_run(run_id, status="paused")
                break
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
    return {"max_rounds": MAX_ROUNDS, "default_workspace": DEFAULT_WORKSPACE, "warning": startup_warning}


@app.get("/api/runs")
def list_runs():
    return db.all_("SELECT * FROM runs ORDER BY id DESC")


@app.post("/api/runs")
def create_run(body: NewRun):
    ws = os.path.abspath(body.workspace.strip().strip('"'))
    os.makedirs(ws, exist_ok=True)
    if git(ws, "rev-parse", "--is-inside-work-tree")[1] != 0:
        git(ws, "init")
    gitignore = os.path.join(ws, ".gitignore")
    if not os.path.exists(gitignore):  # auto-commit uses add -A; keep caches and secrets out of history
        with open(gitignore, "w", encoding="utf-8") as f:
            f.write("__pycache__/\n*.pyc\nnode_modules/\n.env\n.venv/\ndist/\nbuild/\n")
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
        await cancel_auto_resume(run_id)  # Aditya is taking over; don't resume behind his back
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
        if res["limited"]:
            await post_limit(run_id, thread["id"], "Planner", res)
        else:
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
        await cancel_auto_resume(run_id)
        if action == "start":
            await post(run_id, main["id"], "system",
                       "Aditya pressed Start. Write the first instruction for the Executor now.", {"to_planner": True})
        await resume_loop(run_id)
    elif action == "cancel_auto":
        await cancel_auto_resume(run_id)
        await post(run_id, main["id"], "system", "Auto-resume cancelled; Resume when you're ready.")
    elif action == "pause":
        if run["status"] != "running":
            raise HTTPException(409, "not running")
        want_loop[run_id] = False
        await post(run_id, main["id"], "system", "Pause requested; stopping after the current step.")
    elif action == "stop":
        await cancel_auto_resume(run_id)
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
