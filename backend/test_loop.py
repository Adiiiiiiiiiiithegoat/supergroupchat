"""Loop-driver checks against a scripted fake `claude`. Run: python -m backend.test_loop"""
import asyncio
import os
import tempfile
import time

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


LIMITED = {"text": "You've hit your limit", "is_error": True, "limited": {"resets_at": None}}  # reset time unknown


def commits(run_id):
    return main.git(db.get_run(run_id)["workspace"], "rev-list", "--count", "HEAD")[0]


async def test_executor_usage_limit_not_counted_or_committed():
    r, t = new_run()
    await main.post(r, t, "planner", "do it\nSTATUS: CONTINUE", {"status": "CONTINUE"})
    before = commits(r)
    await drive_with(r, ("executor", LIMITED))
    run = db.get_run(r)
    assert (run["status"], run["round"], commits(r)) == ("paused", 0, before)
    assert run["auto_resume_at"] is None and r not in main.auto_resumes  # unknown reset: wait for Aditya
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


async def test_new_workspace_gets_gitignore_in_initial_commit():
    r, _ = new_run()
    ws = db.get_run(r)["workspace"]
    assert main.git(ws, "ls-files")[0] == ".gitignore"
    assert "__pycache__/" in open(os.path.join(ws, ".gitignore")).read()
    keep = os.path.join(tmp, "has_gitignore")
    os.makedirs(keep)
    open(os.path.join(keep, ".gitignore"), "w").write("mine\n")
    main.create_run(main.NewRun(workspace=keep))
    assert open(os.path.join(keep, ".gitignore")).read() == "mine\n"


async def test_startup_pauses_running_runs():
    a, _ = new_run()
    b, _ = new_run()
    db.update_run(a, status="running")
    db.update_run(b, status="done")
    main.reset_stale_runs()
    assert (db.get_run(a)["status"], db.get_run(b)["status"]) == ("paused", "done")


main.AUTO_RESUME_GRACE = 0
OK, DONE = ("executor", {"text": "did it"}), ("planner", {"text": "ok\nSTATUS: DONE"})


def limited_for(seconds):
    return {"text": "You've hit your limit", "is_error": True, "limited": {"resets_at": time.time() + seconds}}


async def limited_run(seconds=0.3):
    """A running loop whose executor hits a usage limit with a known reset time."""
    r, t = new_run()
    await main.post(r, t, "planner", "do it\nSTATUS: CONTINUE", {"status": "CONTINUE"})
    await drive_with(r, ("executor", limited_for(seconds)))
    run = db.get_run(r)
    assert run["status"] == "paused" and run["auto_resume_at"] and r in main.auto_resumes
    return r


async def settle(r):
    if r in main.drivers:
        await main.drivers[r]


async def test_auto_resume_fires_after_reset():
    r = await limited_run()
    script[:] = [OK, DONE]
    await main.auto_resumes[r]
    await settle(r)
    run = db.get_run(r)
    assert (run["status"], run["round"], run["auto_resume_at"]) == ("done", 1, None)
    assert not script


async def test_auto_resume_cancelled_by_button_stop_message_or_manual_resume():
    triggers = {
        "cancel_auto": ([], lambda r: main.control(r, "cancel_auto"), "paused"),
        "stop": ([], lambda r: main.control(r, "stop"), "stopped"),
        "message": ([("planner", {"text": "noted\nSTATUS: NEED_ADITYA"})],
                    lambda r: main.message(r, main.Msg(text="hold on")), "paused"),
        "resume": ([OK, DONE], lambda r: main.control(r, "resume"), "done"),
    }
    for name, (turns, trigger, final) in triggers.items():
        r = await limited_run(0.5)
        task = main.auto_resumes[r]
        script[:] = turns
        await trigger(r)
        await settle(r)
        await asyncio.sleep(0.8)  # well past the reset: a surviving auto-resume would have fired by now
        await settle(r)
        run = db.get_run(r)
        assert task.cancelled() and r not in main.auto_resumes, name
        assert (run["status"], run["auto_resume_at"]) == (final, None), (name, run["status"])
        assert not script, name


async def test_restart_reschedules_auto_resume():
    r = await limited_run()
    main.auto_resumes.pop(r).cancel()  # the server died: in-memory task gone, time still on the run
    assert db.get_run(r)["auto_resume_at"]
    script[:] = [OK, DONE]
    main.reschedule_auto_resumes()
    await main.auto_resumes[r]
    await settle(r)
    assert db.get_run(r)["status"] == "done"


async def test_archive_and_delete_run_keep_workspace():
    r, t = new_run()
    await main.post(r, t, "aditya", "hi")
    ws = db.get_run(r)["workspace"]
    await main.control(r, "archive")
    assert db.get_run(r)["archived"] == 1
    listed = next(x for x in main.list_runs() if x["id"] == r)
    assert listed["last_activity"] == db.all_("SELECT created_at FROM messages WHERE run_id=?", r)[-1]["created_at"]
    await main.control(r, "unarchive")
    assert db.get_run(r)["archived"] == 0
    db.update_run(r, status="running")
    try:
        await main.control(r, "delete")
        assert False, "deleted a running run"
    except main.HTTPException as e:
        assert e.status_code == 409
    db.update_run(r, status="stopped")
    await main.control(r, "delete")
    assert db.get_run(r) is None
    assert not db.all_("SELECT id FROM threads WHERE run_id=?", r) + db.all_("SELECT id FROM messages WHERE run_id=?", r)
    assert main.git(ws, "rev-list", "--count", "HEAD")[0] == "1"  # workspace and its history untouched


async def run_all():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            await fn()
            print("ok", name)


if __name__ == "__main__":
    asyncio.run(run_all())
