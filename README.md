# supergroupchat

Group chat between you, a Planner Claude and Claude Code, running a build loop you can pause and branch. See CLAUDE.md for the full design.

Setup:
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    copy .env.example .env
    python -m backend.auth_setup
    run.bat
Then open http://localhost:8787 (binds 127.0.0.1 only) and log in.

## Login

Single user: password (argon2 hash) + TOTP code. `python -m backend.auth_setup` asks for a password, shows a QR code
for your authenticator app, checks one code, and writes `PASSWORD_HASH`, `TOTP_SECRET`, `SESSION_SECRET` into `.env`.
Re-running it replaces them and logs out every session. Run it in a normal terminal (it asks for input).

- Every route and the websocket need a session, except `GET/POST /login`. Sessions last 7 days and are stored hashed in
  the `sessions` table: **Log out** deletes yours; `DELETE FROM sessions` in `data/sgc.db` kills all of them.
- POSTs and the websocket must have `Origin` = `https://groupchat.solutionhaven.net` or `http://localhost:<PORT>` /
  `http://127.0.0.1:<PORT>`.
- 5 failed logins from one IP (`CF-Connecting-IP`, so the real client behind the tunnel) in 15 minutes lock that IP out
  for 15 minutes. Every failed attempt is printed and appended to `data/auth.log`.
- `ALLOW_LOCAL=1` skips login for loopback requests without `CF-Connecting-IP`. cloudflared also connects from
  127.0.0.1, but Cloudflare always adds that header, so tunnel traffic still needs a login.
- Lockouts and the "TOTP code already used" marker are in memory; a restart clears them.
- Tests: `python -m backend.test_auth`

## Remote access (Cloudflare Tunnel)

`start-all.bat` starts the server and `cloudflared tunnel run --token %TUNNEL_TOKEN%` (token from `.env`). The tunnel
"supergroupchat" is remotely managed: its ingress (groupchat.solutionhaven.net → http://localhost:8787, else 404)
lives in Cloudflare, not in a local config file.

Always-on instead (from an admin terminal; not installed by default):

    cloudflared service install <TUNNEL_TOKEN>

That runs only the tunnel as a Windows service; the server itself still needs `run.bat` (or a scheduled task).
`cloudflared service uninstall` removes it.

Use: **New run** (pick a folder or a recent one), describe what you want, then **Start building**. The one header
button follows the run state (Start building / Pause / Resume); Stop, Archive and Delete are in the ⋯ menu (Delete
only removes the run from the app, never the workspace). Hover a message (long-press on a phone) and choose
**Ask about this** for a side thread; nothing is created until you send the first question. Closing it offers
Discard or Send note to main chat (the note is editable first). Closed side threads reopen read-only from the chip
under their message. Theme (System/Light/Dark) and Log out are in the account menu.

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
- Auto-resume: if the loop was running and the reset time is known, the run auto-resumes 60s after the reset. The
  top bar shows "Auto-resuming at …" with a Cancel button. Stop, manual Start/Resume, or a main-thread message also
  cancel it. The time is stored on the run (`auto_resume_at`) and re-scheduled on server start; an overdue one fires
  right away. If the reset time is unknown, it just pauses and waits for you.
- Round cap sets status `stopped`. Planner `NEED_ADITYA` sets `paused` (reply, then Resume).
- While paused, an executor report waits for Resume before the Planner reviews it, unless you send a message (then
  the Planner gets the report and your message together).
- Workspaces with no commits get an empty `sgc: initial` commit so `git diff --stat HEAD~1` works from round 1.
