"""Loop-driver checks against a scripted fake `claude`. Run: python -m backend.test_loop"""
import asyncio
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ["SGC_DB"] = os.path.join(tmp, "test.db")
from . import db, main  # noqa: E402  (must import after SGC_DB is set)

script, prompts = [], []


async def fake_turn(role, prompt, cwd, **kw):
    assert script, f"unexpected {role} turn (driver retried?)"
    want, res = script.pop(0)
    assert role == want, (role, want)
    prompts.append(prompt)
    return {"session_id": None, "tools": [], "is_error": False, "limited": None, **res}


main.run_turn = fake_turn


def new_run():
    ws = os.path.join(tmp, f"ws{len(db.all_('SELECT id FROM runs'))}")
    run_id = main.create_run(main.NewRun(workspace=ws))["id"]
    return run_id, db.main_thread(run_id)["id"]


def roles(run_id):
    return [m["role"] for m in db.all_("SELECT role FROM messages WHERE run_id=? ORDER BY id", run_id)]


async def drive_with(run_id, *turns, loop=True):
    script[:] = turns
    prompts.clear()
    main.want_loop[run_id] = loop
    if loop:
        await main.set_run(run_id, status="running")
    await main.drive(run_id)
    assert not script, f"turns never ran: {script}"


async def test_planner_error_keeps_executor_report_pending():
    r, t = new_run()
    await main.post(r, t, "planner", "do it\nSTATUS: CONTINUE", {"status": "CONTINUE"})
    await drive_with(r, ("executor", {"text": "did it"}), ("planner", {"text": "boom", "is_error": True}))
    assert db.get_run(r)["status"] == "paused"
    assert roles(r)[-1] == "system"
    assert [m["role"] for m in main.pending_for_planner(r, t)] == ["executor"]
    await drive_with(r, ("planner", {"text": "looks good\nSTATUS: DONE"}))  # Resume
    assert "Executor report (round 1)" in prompts[0] and "did it" in prompts[0]
    assert db.get_run(r)["status"] == "done"


async def test_planner_error_while_chatting_doesnt_retry():
    r, t = new_run()
    await main.post(r, t, "aditya", "hi")
    await drive_with(r, ("planner", {"text": "boom", "is_error": True}), loop=False)
    assert db.get_run(r)["status"] == "discussing"
    assert [m["role"] for m in main.pending_for_planner(r, t)] == ["aditya"]


LIMITED = {"text": "You've hit your limit", "is_error": True, "limited": {"resets_at": 1793652000}}


def commits(run_id):
    return main.git(db.get_run(run_id)["workspace"], "rev-list", "--count", "HEAD")[0]


async def test_executor_usage_limit_not_counted_or_committed():
    r, t = new_run()
    await main.post(r, t, "planner", "do it\nSTATUS: CONTINUE", {"status": "CONTINUE"})
    before = commits(r)
    await drive_with(r, ("executor", LIMITED))
    run = db.get_run(r)
    assert (run["status"], run["round"], commits(r)) == ("paused", 0, before)
    assert roles(r)[-1] == "system" and "usage limit" in db.all_("SELECT content FROM messages WHERE run_id=?", r)[-1]["content"]
    assert "executor" not in roles(r)
    await drive_with(r, ("executor", {"text": "did it"}), ("planner", {"text": "ok\nSTATUS: DONE"}))  # Resume
    assert prompts[0] == "do it"  # same instruction rerun
    assert db.get_run(r)["round"] == 1 and commits(r) == str(int(before) + 1)


async def test_planner_usage_limit_pauses_and_keeps_pending():
    r, t = new_run()
    await main.post(r, t, "planner", "do it\nSTATUS: CONTINUE", {"status": "CONTINUE"})
    await drive_with(r, ("executor", {"text": "did it"}), ("planner", LIMITED))
    assert db.get_run(r)["status"] == "paused"
    assert [m["role"] for m in main.pending_for_planner(r, t)] == ["executor"]


async def run_all():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            await fn()
            print("ok", name)


if __name__ == "__main__":
    asyncio.run(run_all())
