"""Package source only; user submissions and local history are never included."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
files = [root / name for name in ('README.md', 'LICENSE', '.gitignore', 'requirements.txt', 'requirements-dev.txt', 'pytest.ini', 'start.bat', 'start.ps1')]
for folder in ('grader', 'scripts', 'tests', 'examples'):
    files.extend(p for p in (root / folder).rglob('*') if p.is_file() and p.suffix in {'.py', '.md', '.html', '.css', '.js'} and '__pycache__' not in p.parts)
destination = root / 'dist' / 'ai-grader-source.zip'
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        archive.write(path, 'ai-grader/' + path.relative_to(root).as_posix())
print(destination)
