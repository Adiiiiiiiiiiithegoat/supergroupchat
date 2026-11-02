"""Drives the `claude` CLI headless: one subprocess per turn, stream-json parsed line by line."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANNER_PROMPT = os.path.join(ROOT, "prompts", "planner.md")
EXECUTOR_PROMPT = os.path.join(ROOT, "prompts", "executor.md")

# Keep Aditya's user-level ~/.claude/CLAUDE.md out of agent sessions; the workspace's own CLAUDE.md still loads.
# --setting-sources doesn't do this in 2.1.316 (tested); claudeMdExcludes via --settings does.
USER_CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
AGENT_SETTINGS = os.path.join(ROOT, "data", "agent-settings.json")
with open(AGENT_SETTINGS, "w", encoding="utf-8") as f:
    json.dump({"claudeMdExcludes": [USER_CLAUDE_MD.replace("\\", "/")]}, f)


def shell_rules(*cmds):
    # On Windows the agents often pick the PowerShell tool over Bash, so allow both.
    return [f"{tool}({c}:*)" for tool in ("Bash", "PowerShell") for c in cmds]


# --append-system-prompt-file isn't in `claude --help` but works (2.1.316); the .cmd shim would cut a
# multi-line --append-system-prompt at the first newline. Prompt text goes via stdin for the same reason.
# --permission-prompts none: anything outside the allowlist is denied instead of hanging a headless turn.
COMMON = ["-p", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none",
          "--settings", AGENT_SETTINGS]
PLANNER_FLAGS = [
    "--append-system-prompt-file", PLANNER_PROMPT,
    "--disallowedTools", "Edit", "Write", "NotebookEdit",
    "--allowedTools", "Read", "Glob", "Grep", *shell_rules("git diff", "git log", "git status", "git show"),
]
EXECUTOR_FLAGS = [
    "--append-system-prompt-file", EXECUTOR_PROMPT,
    "--permission-mode", "acceptEdits",
    "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
    *shell_rules("git status", "git diff", "git log", "git show", "python", "py", "pip",
                 "pytest", "npm", "npx", "node", "ls", "mkdir", "cat"),
]

STATUS_RE = re.compile(r"STATUS:\W*(CONTINUE|DONE|NEED_ADITYA)")
LIMIT_RE = re.compile(r"(usage|rate)[ _-]?limit|hit your (usage )?limit|limit (reached|exceeded)", re.I)


def usage_limit(result, rate_info, text):
    """None if the turn wasn't stopped by a usage/rate limit, else {"resets_at": unix ts or None}."""
    rejected = (rate_info or {}).get("status") == "rejected"
    is_error = result is None or bool(result.get("is_error"))
    if rejected or (is_error and ((result or {}).get("api_error_status") == 429 or LIMIT_RE.search(text or ""))):
        return {"resets_at": (rate_info or {}).get("resetsAt")}
    return None


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
    """Run one agent turn. Returns {text, session_id, is_error, tools, limited}."""
    args = [claude_exe(), *COMMON, *(PLANNER_FLAGS if role == "planner" else EXECUTOR_FLAGS)]
    if resume:
        args += ["--resume", resume] + (["--fork-session"] if fork else [])
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # Pro login only, never API billing
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, limit=64 * 1024 * 1024,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    if on_proc:
        on_proc(proc)
    result, rate_info, tools, version = None, None, [], None
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
            elif ev.get("type") == "system" and ev.get("subtype") == "init":
                version = ev.get("claude_code_version")
            elif ev.get("type") == "rate_limit_event":
                rate_info = ev.get("rate_limit_info")
            elif ev.get("type") == "result":
                result = ev
        await proc.wait()
    finally:
        if proc.returncode is None:
            kill_tree(proc.pid)
    err = (await stderr_task).decode("utf-8", "replace").strip()
    if result is None:
        text = f"[{role} exited with code {proc.returncode} and no result]\n{err[-2000:]}"
        return {"text": text, "session_id": None, "is_error": True, "tools": tools,
                "limited": usage_limit(None, rate_info, text), "version": version}
    text = result.get("result") or ""
    return {"text": text, "session_id": result.get("session_id"), "is_error": bool(result.get("is_error")),
            "tools": tools, "limited": usage_limit(result, rate_info, text), "version": version}


SELFCHECK_PROMPT = """Startup self-check. Do not use any tools; answer from the CLAUDE.md contents already in your context.
Line 1: the self-check marker from the workspace CLAUDE.md, verbatim, or NONE.
Line 2: USER_CLAUDE_MD=YES if your context includes the contents of a user-level CLAUDE.md (the one in the \
user's home .claude folder, the user's private global instructions), otherwise USER_CLAUDE_MD=NO.
Line 3: only if YES, the first line of that user-level file, verbatim."""


def selfcheck_verdict(text, marker, user_first_line):
    """None if the exclusion held, else a warning."""
    if marker not in text:
        return "CLAUDE.md self-check was inconclusive: the agent didn't echo the workspace CLAUDE.md marker."
    if "USER_CLAUDE_MD=YES" in text or user_first_line in text:
        return "Your user-level ~/.claude/CLAUDE.md is loading into agent sessions (claudeMdExcludes stopped working?)."
    if "USER_CLAUDE_MD=NO" not in text:
        return "CLAUDE.md self-check was inconclusive: no USER_CLAUDE_MD answer."
    return None


async def check_claude_md_exclusion():
    """One real planner turn in a temp folder with the normal flags. Returns (warning or None, CLI version)."""
    try:
        with open(USER_CLAUDE_MD, encoding="utf-8") as f:
            first_line = next(l.strip() for l in f if l.strip())
    except (FileNotFoundError, StopIteration):
        return None, "not checked (no user CLAUDE.md)"
    marker = f"SGC-SELFCHECK-{uuid.uuid4().hex[:12]}"
    tmp = tempfile.mkdtemp(prefix="sgc-selfcheck-")
    try:
        with open(os.path.join(tmp, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write(f"Self-check marker: {marker}\n")
        res = await run_turn("planner", SELFCHECK_PROMPT, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if res["is_error"]:
        return f"CLAUDE.md self-check errored: {res['text'][:300]}", res["version"] or "unknown"
    return selfcheck_verdict(res["text"], marker, first_line), res["version"] or "unknown"


if __name__ == "__main__":
    assert parse_status("do X\n\nSTATUS: CONTINUE") == "CONTINUE"
    assert parse_status("**STATUS: DONE**") == "DONE"
    assert parse_status("STATUS: CONTINUE\nactually\nSTATUS: NEED_ADITYA") == "NEED_ADITYA"
    assert parse_status("no status here") == "NEED_ADITYA"
    assert strip_status("Edit hello.py\nSTATUS: CONTINUE") == "Edit hello.py"
    assert tool_summary({"name": "Bash", "input": {"command": "git status"}}) == "Bash: git status"
    ok = {"is_error": False}
    assert usage_limit(ok, {"status": "allowed", "resetsAt": 1}, "fine") is None
    assert usage_limit(ok, {"status": "rejected", "resetsAt": 1}, "") == {"resets_at": 1}
    assert usage_limit({"is_error": True, "api_error_status": 429}, None, "x") == {"resets_at": None}
    assert usage_limit({"is_error": True}, None, "You've hit your limit · resets 3pm") is not None
    assert usage_limit({"is_error": True}, None, "Claude AI usage limit reached|1700000000") is not None
    assert usage_limit({"is_error": True}, None, "API Error: 500 overloaded") is None
    assert usage_limit(ok, None, "we should add rate limiting to the API") is None  # normal text, not an error
    m, first = "SGC-SELFCHECK-abc", "# Public API lookup"
    assert selfcheck_verdict(f"{m}\nUSER_CLAUDE_MD=NO\nSTATUS: NEED_ADITYA", m, first) is None
    assert "loading" in selfcheck_verdict(f"{m}\nUSER_CLAUDE_MD=YES\n{first}", m, first)
    assert "loading" in selfcheck_verdict(f"{m}\nUSER_CLAUDE_MD=NO\n{first}", m, first)  # quoted it anyway
    assert "inconclusive" in selfcheck_verdict("NONE\nUSER_CLAUDE_MD=NO", m, first)
    assert "inconclusive" in selfcheck_verdict(f"{m}\nnot sure", m, first)
    print("ok")
