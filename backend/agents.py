"""Drives the `claude` CLI headless: one subprocess per turn, stream-json parsed line by line."""
import asyncio
import json
import os
import re
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANNER_PROMPT = os.path.join(ROOT, "prompts", "planner.md")
EXECUTOR_PROMPT = os.path.join(ROOT, "prompts", "executor.md")

# --append-system-prompt-file isn't in `claude --help` but works (2.1.316); the .cmd shim would cut a
# multi-line --append-system-prompt at the first newline. Prompt text goes via stdin for the same reason.
# --permission-prompts none: anything outside the allowlist is denied instead of hanging a headless turn.
COMMON = ["-p", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
PLANNER_FLAGS = [
    "--append-system-prompt-file", PLANNER_PROMPT,
    "--disallowedTools", "Edit", "Write", "NotebookEdit",
    "--allowedTools", "Read", "Glob", "Grep",
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git status:*)", "Bash(git show:*)",
]
EXECUTOR_FLAGS = [
    "--append-system-prompt-file", EXECUTOR_PROMPT,
    "--permission-mode", "acceptEdits",
    "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
    *[f"Bash({c}:*)" for c in ("git status", "git diff", "git log", "git show", "python", "py", "pip",
                               "pytest", "npm", "npx", "node", "ls", "mkdir", "cat")],
]

STATUS_RE = re.compile(r"STATUS:\W*(CONTINUE|DONE|NEED_ADITYA)")


def parse_status(text):
    """Last STATUS line wins; missing status is treated as NEED_ADITYA (safe stop)."""
    found = STATUS_RE.findall(text or "")
    return found[-1] if found else "NEED_ADITYA"


def strip_status(text):
    return "\n".join(l for l in text.splitlines() if not STATUS_RE.search(l)).strip()


def tool_summary(block):
    inp = block.get("input") or {}
    detail = next((str(inp[k]) for k in ("command", "file_path", "pattern", "path", "description") if k in inp),
                  json.dumps(inp))
    return f"{block.get('name')}: {detail[:160]}"


def claude_exe():
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("`claude` not found on PATH")
    return exe


def kill_tree(pid):
    # claude on Windows is a .cmd shim; terminate() alone would orphan the real process.
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


async def run_turn(role, prompt, cwd, *, resume=None, fork=False, on_text=None, on_tool=None, on_proc=None):
    """Run one agent turn. Returns {text, session_id, is_error, tools}."""
    args = [claude_exe(), *COMMON, *(PLANNER_FLAGS if role == "planner" else EXECUTOR_FLAGS)]
    if resume:
        args += ["--resume", resume] + (["--fork-session"] if fork else [])
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # Pro login only, never API billing
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, limit=64 * 1024 * 1024,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    if on_proc:
        on_proc(proc)
    result, tools = None, []
    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        async for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in ev["message"].get("content", []):
                    if block.get("type") == "text" and block.get("text") and on_text:
                        await on_text(block["text"])
                    elif block.get("type") == "tool_use":
                        tools.append(tool_summary(block))
                        if on_tool:
                            await on_tool(tools[-1])
            elif ev.get("type") == "result":
                result = ev
        await proc.wait()
    finally:
        if proc.returncode is None:
            kill_tree(proc.pid)
    err = (await stderr_task).decode("utf-8", "replace").strip()
    if result is None:
        return {"text": f"[{role} exited with code {proc.returncode} and no result]\n{err[-2000:]}",
                "session_id": None, "is_error": True, "tools": tools}
    return {"text": result.get("result") or "", "session_id": result.get("session_id"),
            "is_error": bool(result.get("is_error")), "tools": tools}


if __name__ == "__main__":
    assert parse_status("do X\n\nSTATUS: CONTINUE") == "CONTINUE"
    assert parse_status("**STATUS: DONE**") == "DONE"
    assert parse_status("STATUS: CONTINUE\nactually\nSTATUS: NEED_ADITYA") == "NEED_ADITYA"
    assert parse_status("no status here") == "NEED_ADITYA"
    assert strip_status("Edit hello.py\nSTATUS: CONTINUE") == "Edit hello.py"
    assert tool_summary({"name": "Bash", "input": {"command": "git status"}}) == "Bash: git status"
    print("ok")
