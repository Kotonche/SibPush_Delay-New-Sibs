"""Build an installable Progressive Siblings .ankiaddon archive."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "dist" / "progressive-siblings.ankiaddon"
ROOT_FILES = ("__init__.py", "config.json", "config.md", "LICENSE", "README.md")


def package_files() -> list[Path]:
    files = [ROOT / filename for filename in ROOT_FILES]
    files.extend(
        path
        for path in (ROOT / "sibpush").rglob("*.py")
        if "__pycache__" not in path.parts
    )
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in package_files():
            archive.write(path, path.relative_to(ROOT).as_posix())
    print(OUTPUT)


if __name__ == "__main__":
    main()
