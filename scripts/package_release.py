"""Build release archives from an allowlist, never from a workspace snapshot."""
import argparse
import tarfile
from pathlib import Path


ROOT_FILES = {
    ".env.example", "README.md", "INSTALL.md", "MAINTENANCE.md",
    "requirements.txt", "requirements.lock", "config.py", "oracleiq.py",
    "run.py", "run_web_only.py", "test_odin.py", "package.sh",
    "package.json", "package-lock.json", "playwright.config.mjs",
}
TREE_SUFFIXES = {
    "api": {".py"},
    "analyzer": {".py"},
    "collector": {".py"},
    "db": {".py"},
    "scripts": {".py", ".mjs"},
    "templates": {".html"},
    "static": {".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2"},
    "tests": {".py", ".mjs"},
}


def release_files(source: Path):
    source = Path(source).resolve()
    for name in sorted(ROOT_FILES):
        path = source / name
        if path.is_file() and not path.is_symlink():
            yield path
    for directory, suffixes in TREE_SUFFIXES.items():
        root = source / directory
        if root.is_symlink() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(source)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if path.suffix not in suffixes or not path.is_file():
                continue
            if not path.resolve().is_relative_to(source):
                continue
            if any(part.is_symlink() for part in (path, *path.parents) if part != source):
                continue
            yield path


def build_archive(source: Path, destination: Path) -> int:
    source = Path(source).resolve(strict=True)
    destination = Path(destination)
    files = list(release_files(source))
    if not files:
        raise ValueError("Aucun fichier distribuable.")
    with destination.open("xb") as output:
        try:
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                for path in files:
                    archive.add(path, arcname=str(Path("oracleiq") / path.relative_to(source)),
                                recursive=False)
        except (OSError, tarfile.TarError):
            output.close()
            destination.unlink(missing_ok=True)
            raise
    return len(files)


def main():
    parser = argparse.ArgumentParser(description="Archive ODIN sans donnees locales")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        count = build_archive(Path(__file__).resolve().parent.parent, args.destination)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"Erreur : {error}\n")
    print(f"Archive creee : {args.destination} ({count} fichiers)")


if __name__ == "__main__":
    main()
