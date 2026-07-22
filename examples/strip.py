import re
from pathlib import Path

# Matches any non-ASCII character
non_ascii = re.compile(r'[^\x00-\x7F]+')

# Directories to skip
SKIP_DIRS = {"venv", ".venv", "env", "__pycache__"}

for path in Path('.').rglob('*.py'):
    # If any parent directory is in SKIP_DIRS, skip this file
    if any(part in SKIP_DIRS for part in path.parts):
        continue

    text = path.read_text(encoding='utf-8')
    cleaned = non_ascii.sub('', text)
    if cleaned != text:
        path.write_text(cleaned, encoding='utf-8')
        print(f"Cleaned {path}")
