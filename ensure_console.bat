@echo off
REM SecTrust 控制台自愈看门狗入口（供 Windows 计划任务调用）。
REM 幂等：服务活着就直接退出，不会重复拉起。
cd /d "%~dp0"
set PY="C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist %PY% set PY=python
%PY% "%~dp0ensure_console.py"
exit /b %errorlevel%
