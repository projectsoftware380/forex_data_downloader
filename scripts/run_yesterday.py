import os, sys, subprocess, datetime
symbols = ["EURUSD","GBPUSD","USDJPY"]
y = datetime.date.today() - datetime.timedelta(days=1)
start, end = y.isoformat(), (y + datetime.timedelta(days=1)).isoformat()
py = r".\.venv311\Scripts\python.exe"
for s in symbols:
    subprocess.run([py,"-m","forex_data.cli","download-ticks","--symbol",s,"--start",start,"--end",end], check=True)
for s in symbols:
    subprocess.run([py,"-m","forex_data.cli","build-bars","--symbol",s,"--start",start,"--end",end], check=True)
