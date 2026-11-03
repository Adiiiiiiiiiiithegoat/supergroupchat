# supergroupchat v1

## Setup
- [x] git init + initial commit of spec
- [x] Verify claude CLI flags against `claude --help`

## Backend
- [x] sqlite schema (runs, threads, messages)
- [x] claude subprocess runner (stream-json parse, taskkill tree)
- [x] Loop driver: discuss → start → executor/planner rounds, auto-commit, diff stat
- [x] STATUS parsing, round cap, pause / stop / resume
- [x] Branch threads with forked planner session
- [x] REST + websocket API

## Frontend
- [x] Timeline, live streaming, top bar controls, composer
- [x] Branch side panel, phone-width layout

## Ship
- [x] run.bat + README notes on flag differences
- [x] Smoke test: real round, pause, branch

## Follow-ups
- [ ] Click-test branch panel + live streaming in a real browser (Chrome extension was down during smoke test)

## Review fixes
- [x] Planner errors posted as system so the executor report stays pending (+ test)
- [x] Pause on usage/rate limit; limited executor round not counted or committed
- [x] Default .gitignore for new workspaces
- [x] Keep user ~/.claude/CLAUDE.md out of agent sessions (claudeMdExcludes)
- [x] Reset 'running' runs to 'paused' on startup

## Hardening
- [x] Startup self-check for the CLAUDE.md exclusion (banner + console, SKIP_STARTUP_CHECK)
- [x] Auto-resume after a usage limit (scheduled, cancellable, survives restart)

## Remote access (groupchat.solutionhaven.net, no Cloudflare Access)
- [x] Password (argon2) + TOTP login, setup script with terminal QR
- [x] Server-side 7-day sessions, signed HttpOnly/Secure/Strict cookie, logout
- [x] Guard every route + websocket; Origin check on POSTs and websocket
- [x] Login lockout (5 fails → 15 min, CF-Connecting-IP), log failures; ALLOW_LOCAL
- [x] Auth tests pass
- [x] Tunnel "supergroupchat" + ingress via API, proxied CNAME groupchat
- [x] cloudflared installed, start-all.bat
- [x] Verify login page, rejected API/websocket, dashboard untouched
- [x] Real login through the tunnel (password + TOTP) end to end

## UI redesign
- [x] Backend: archive/delete runs, last activity, speaking state (+ test)
- [x] Prompts: no DONE without verification; Executor stops reporting commit status
- [x] Warm palette, light/dark + toggle, serif agent text, sidebar/drawer
- [x] Header: phase stepper, round, one primary action, ⋯ and account menus
- [x] Messages: cards/bubbles, status pills, markdown, tools/diff summaries, round dividers, needs-you bar
- [x] Live typing with timer + latest tool, smart auto-scroll, jump to latest
- [x] Side threads: Ask about this, draft until first send, close with editable note, reopen read-only
- [x] Screenshots desktop/390px light/dark + headless interaction checks
- [ ] Check on the real phone (touch long-press, iOS Safari)
