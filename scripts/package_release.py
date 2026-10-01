"""Build a source archive from an allowlist; never glob user spreadsheets."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
files = ["README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements.txt", "requirements-dev.txt",
         "pytest.ini", "start.bat", "start.ps1", "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore",
         ".github/workflows/tests.yml", "scripts/package_release.py"]
for folder, extensions in (("app", {".py", ".html", ".css", ".js"}), ("tests", {".py"})):
    files.extend(str(p.relative_to(root)) for p in (root / folder).rglob("*")
                 if p.is_file() and p.suffix in extensions and p.name != "manual_server.py")
files.append("app/static/example.csv")
destination = root / "dist" / "canvas-group-grader.zip"
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for name in sorted(set(files)):
        archive.write(root / name, "canvas-group-grader/" + Path(name).as_posix())
print(destination)
