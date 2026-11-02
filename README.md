# supergroupchat

Group chat between you, a Planner Claude and Claude Code, running a build loop you can pause and branch. See CLAUDE.md for the full design.

Setup:
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    copy .env.example .env
    run.bat
Then open http://localhost:8787 (binds 127.0.0.1 only; there is no auth).

Use: type a workspace folder, **New run**, chat with the Planner, **Start loop**. Click any message to open a side thread.
"Close & send note" sends whatever is in the side-thread box as a one-line note into the main thread.

Self-check for the status/tool parsing: `python -m backend.agents`

## Differences from the spec (checked against Claude Code 2.1.316)

All spec flags exist. Changes:
- System prompts use `--append-system-prompt-file prompts/*.md` (works, but isn't listed in `--help`). `claude` resolves
  to `claude.cmd`, and cmd.exe cuts a multi-line `--append-system-prompt` argument at the first newline.
- The prompt always goes via stdin, never argv: same newline problem, and `--allowedTools` is variadic so a trailing
  positional prompt would be eaten as a tool name.
- Added `--permission-prompts none`, so a tool outside the allowlist is denied instead of hanging a headless turn.
- Your user-level `~/.claude/CLAUDE.md` is kept out of Planner/Executor sessions. `--setting-sources project,local`
  does **not** do this in 2.1.316 (tested: the user CLAUDE.md still loaded). The backend writes
  `data/agent-settings.json` with `{"claudeMdExcludes": ["C:/Users/<you>/.claude/CLAUDE.md"]}` and passes it via
  `--settings`. Tested with a real turn: the user file is gone, the workspace's own CLAUDE.md still loads.
  (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` also exists but drops the workspace CLAUDE.md too.)
  Because `claudeMdExcludes` is undocumented, every server start runs a background self-check: one short Planner
  turn with the normal flags in a temp folder whose CLAUDE.md holds a random marker. The agent must echo the marker,
  answer `USER_CLAUDE_MD=NO`, and not quote your CLAUDE.md's first line. Otherwise a warning banner appears on the
  page (also returned in `/api/config`) and is printed in the console with the CLI version. `SKIP_STARTUP_CHECK=1` in
  `.env` turns it off.
- `ANTHROPIC_API_KEY` is stripped from the child env so turns always use the Claude Pro login.
- Sessions use the CLI default `--system-prompt-snapshot on`: an edit to `prompts/*.md` only affects new runs.
- Usage/rate limits: a turn counts as limited if the stream's `rate_limit_event` says `rejected`, or the result is an
  error with `api_error_status` 429 or limit wording ("usage limit", "hit your limit", ...). The run pauses with the
  reset time. A limited Executor turn isn't counted as a round or committed; Resume reruns the same instruction.
  This was tested with simulated results only, because I couldn't trigger a real limit, so the exact wording a real
  limit produces is a best guess.
- Round cap sets status `stopped`. Planner `NEED_ADITYA` sets `paused` (reply, then Resume).
- While paused, an executor report waits for Resume before the Planner reviews it, unless you send a message (then
  the Planner gets the report and your message together).
- Workspaces with no commits get an empty `sgc: initial` commit so `git diff --stat HEAD~1` works from round 1.
