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
- [ ] Timeline, live streaming, top bar controls, composer
- [ ] Branch side panel, phone-width layout

## Ship
- [ ] run.bat + README notes on flag differences
- [ ] Smoke test: real round, pause, branch
