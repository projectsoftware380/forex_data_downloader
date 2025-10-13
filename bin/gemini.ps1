param([Parameter(ValueFromRemainingArguments = $true)] $Args)
& ".\.venv311\Scripts\python.exe" ".\gemini.py" @Args
