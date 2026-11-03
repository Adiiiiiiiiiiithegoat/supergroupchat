@echo off
rem Server in its own window, tunnel in this one. TUNNEL_TOKEN comes from .env (gitignored).
cd /d "%~dp0"
set TUNNEL_TOKEN=
for /f "usebackq tokens=1,* delims==" %%a in (".env") do if "%%a"=="TUNNEL_TOKEN" set TUNNEL_TOKEN=%%b
if defined TUNNEL_TOKEN set TUNNEL_TOKEN=%TUNNEL_TOKEN:"=%
if defined TUNNEL_TOKEN set TUNNEL_TOKEN=%TUNNEL_TOKEN:'=%
if not defined TUNNEL_TOKEN (echo TUNNEL_TOKEN missing from .env & exit /b 1)
set CF=cloudflared
where cloudflared >nul 2>&1 || set CF="%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
start "supergroupchat server" cmd /k "%~dp0run.bat"
%CF% tunnel --no-autoupdate run --token %TUNNEL_TOKEN%
