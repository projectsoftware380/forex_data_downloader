$ErrorActionPreference="Stop"
$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"
$py=".\.venv311\Scripts\python.exe"
& $py .\scripts\run_yesterday.py *>> ".\logs\run_yesterday.log"
