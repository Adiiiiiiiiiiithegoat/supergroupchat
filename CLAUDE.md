# supergroupchat

A private, local web app: a group chat between **Aditya**, a **Planner** (Claude Code session, read-only) and an **Executor** (Claude Code session that edits code). The Planner and Executor hand work back and forth in a loop while Aditya watches, pauses, and asks questions.

Runs on Aditya's Windows 11 laptop. Auth is Claude Pro (Claude Code login), so **there is no Anthropic API key**. Both agents are driven by spawning the `claude` CLI in headless mode. Do not add the Anthropic SDK or any API key handling.

## The flow

1. **Discuss** – Aditya chats with the Planner about what to build. Planner can read the target workspace but cannot edit.
2. **Kickoff** – Aditya presses Start. Planner writes the first instruction for the Executor.
3. **Loop** – each round:
   - Executor runs the instruction, returns its final response.
   - Backend auto-commits the workspace (`sgc: round N`) and collects `git diff --stat HEAD~1`.
   - Planner receives: Executor's response + the diff stat. It replies with the next instruction.
   - Planner ends every message with exactly one status line: `STATUS: CONTINUE`, `STATUS: DONE`, or `STATUS: NEED_ADITYA`. Only CONTINUE keeps the loop going.
   - A hard round cap (config `MAX_ROUNDS`, default 15) stops the loop.
4. **Pause / Stop / Resume**
   - Pause = stop after the current step finishes (flag checked between steps).
   - Stop = kill the running process tree now. On Windows `claude` is a `.cmd` shim, so use `taskkill /T /F /PID <pid>`; `proc.terminate()` alone orphans the node child.
   - Resume = continue the loop from where it paused.
   - While paused, Aditya's messages go to the main Planner; the Planner can adjust the next instruction.
5. **Branch** – Aditya clicks any message → side thread. Backend forks the Planner session (`--resume <planner_session> --fork-session`) so questions don't pollute the main Planner context. Closing a branch: discard, or send a one-line note back into the main thread as a user message.

## Driving Claude Code

- Each turn = one subprocess: `claude -p <prompt> --output-format stream-json --verbose [--resume <session_id>] ...`, `cwd` = the target workspace.
- Parse stream-json line by line: forward assistant text and tool-use summaries to the UI live; the final `result` event has `result` (final text) and `session_id`. Store session ids.
- **Executor flags:** `--permission-mode acceptEdits`, `--allowedTools` with a sensible allowlist (Read, Edit, Write, Glob, Grep, and specific Bash commands like git, python, pip, npm, node). Never `--dangerously-skip-permissions`. `--append-system-prompt` from `prompts/executor.md`.
- **Planner flags:** `--disallowedTools Edit Write NotebookEdit`, `--allowedTools Read Glob Grep "Bash(git diff:*)" "Bash(git log:*)" "Bash(git status:*)" "Bash(git show:*)"`, `--append-system-prompt` from `prompts/planner.md`.
- **Verify every flag against `claude --help` on this machine before relying on it.** If a flag differs, adapt and note it in README.
- Prompts can be long; pass via stdin if argv length becomes a problem on Windows.
- Resolve the binary with `shutil.which("claude")`.

## Workspaces

supergroupchat is the tool, not the thing being built. Each **run** targets a workspace directory (any folder, e.g. `D:\Projects\Formula 0.1`). Backend `git init`s it if it isn't a repo. One active run at a time is fine for v1.

## Stack

- Python 3.11+, FastAPI, uvicorn, websockets, stdlib `sqlite3` (db at `data/sgc.db`).
- Windows asyncio: subprocesses need the Proactor event loop. Run uvicorn **without** `--reload` (reload mode can switch to the selector loop and break subprocess spawning). Provide `run.bat` / `python -m backend.main`.
- Frontend: single `frontend/index.html` + vanilla JS/CSS served by FastAPI. No build step.
- Config via `.env` (see `.env.example`): `PORT`, `MAX_ROUNDS`, `DEFAULT_WORKSPACE`.

## Data model (sqlite)

- `runs(id, workspace, status[discussing|running|paused|done|stopped], round, planner_session, executor_session, created_at)`
- `threads(id, run_id, kind[main|branch], parent_message_id, planner_session, status[open|closed])`
- `messages(id, run_id, thread_id, role[aditya|planner|executor|system], content, meta_json, created_at)` – `meta_json` holds diff stats, round number, tool-use summaries.

## UI

- Main timeline, color-coded by role (Aditya / Planner / Executor / system). Executor tool calls shown collapsed.
- Live streaming of the agent currently speaking, with a "typing" state.
- Top bar: workspace, run status, round N/MAX, buttons: Start loop, Pause, Stop, Resume.
- Composer at the bottom (talks to main Planner).
- Click a message → branch panel slides in on the right with its own composer and "Close (discard)" / "Close & send note" buttons.
- Works on a phone-width screen (will be opened remotely later).

## Later (not v1)

Cloudflare Tunnel + Cloudflare Access to expose at a subdomain; usage-aware throttling; multiple concurrent runs.
