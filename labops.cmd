@echo off
rem LabOps launcher: always runs the integrated main checkout (this folder's src\).
rem Uses .venv\Scripts\python.exe when present (M0b+ needs the pinned mcp package), else python on PATH.
setlocal
set "LABOPS_ROOT=%~dp0"
set "LABOPS_PY=python"
if exist "%LABOPS_ROOT%.venv\Scripts\python.exe" set "LABOPS_PY=%LABOPS_ROOT%.venv\Scripts\python.exe"
set "PYTHONPATH=%LABOPS_ROOT%src;%PYTHONPATH%"
rem Hook fast path: `labops hook ingest --client X` must not import the CLI, sqlite or mcp (SessionEnd budget 1-3 s).
if /i "%~1"=="hook" (
  "%LABOPS_PY%" -m labops.hook %2 %3 %4 %5
  exit /b 0
)
"%LABOPS_PY%" -m labops.cli %*
exit /b %ERRORLEVEL%
