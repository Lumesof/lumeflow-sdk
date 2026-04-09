import argparse
import json
import os
import stat
import sys
import tarfile


def _getMtime() -> int:
    sourceDateEpoch = os.environ.get("SOURCE_DATE_EPOCH")
    if sourceDateEpoch and sourceDateEpoch.isdigit():
        return int(sourceDateEpoch)
    return 0


def _normalizeTarPath(path: str) -> str:
    return path.lstrip("/")


def _applyCommonMetadata(tarInfo: tarfile.TarInfo, mtime: int) -> None:
    tarInfo.mtime = mtime
    tarInfo.uid = 0
    tarInfo.gid = 0
    tarInfo.uname = "root"
    tarInfo.gname = "root"


def _ensureParentDirs(tf: tarfile.TarFile, destPath: str, mtime: int, writtenPaths: set[str]) -> None:
    normalized = _normalizeTarPath(destPath)
    if not normalized:
        return

    parts = normalized.split("/")[:-1]
    current = ""
    for part in parts:
        if not part:
            continue
        current = f"{current}/{part}" if current else part
        if current in writtenPaths:
            continue

        tarInfo = tarfile.TarInfo(current)
        tarInfo.type = tarfile.DIRTYPE
        tarInfo.mode = 0o755
        _applyCommonMetadata(tarInfo, mtime)
        tf.addfile(tarInfo)
        writtenPaths.add(current)


def _addDirectory(tf: tarfile.TarFile, destPath: str, mode: int, mtime: int, writtenPaths: set[str]) -> None:
    normalized = _normalizeTarPath(destPath.rstrip("/"))
    if not normalized or normalized in writtenPaths:
        return

    _ensureParentDirs(tf, normalized, mtime, writtenPaths)

    tarInfo = tarfile.TarInfo(normalized)
    tarInfo.type = tarfile.DIRTYPE
    tarInfo.mode = mode
    _applyCommonMetadata(tarInfo, mtime)
    tf.addfile(tarInfo)
    writtenPaths.add(normalized)


def _addSymlink(tf: tarfile.TarFile, destPath: str, target: str, mtime: int, writtenPaths: set[str]) -> None:
    normalized = _normalizeTarPath(destPath)
    if not normalized or normalized in writtenPaths:
        return

    _ensureParentDirs(tf, normalized, mtime, writtenPaths)

    tarInfo = tarfile.TarInfo(normalized)
    tarInfo.type = tarfile.SYMTYPE
    tarInfo.linkname = target
    tarInfo.mode = 0o777
    _applyCommonMetadata(tarInfo, mtime)
    tf.addfile(tarInfo)
    writtenPaths.add(normalized)


def _addFile(tf: tarfile.TarFile, sourcePath: str, destPath: str, mtime: int, writtenPaths: set[str]) -> None:
    normalized = _normalizeTarPath(destPath)
    if not normalized or normalized in writtenPaths:
        return

    _ensureParentDirs(tf, normalized, mtime, writtenPaths)

    sourceStat = os.lstat(sourcePath)
    tarInfo = tarfile.TarInfo(normalized)
    tarInfo.size = sourceStat.st_size
    tarInfo.mode = stat.S_IMODE(sourceStat.st_mode)
    _applyCommonMetadata(tarInfo, mtime)

    with open(sourcePath, "rb") as sourceFile:
        tf.addfile(tarInfo, sourceFile)

    writtenPaths.add(normalized)


def _addPathRecursive(tf: tarfile.TarFile, sourcePath: str, destPath: str, mtime: int, writtenPaths: set[str]) -> None:
    sourceStat = os.lstat(sourcePath)

    if stat.S_ISLNK(sourceStat.st_mode):
        _addSymlink(tf, destPath, os.readlink(sourcePath), mtime, writtenPaths)
        return

    if stat.S_ISDIR(sourceStat.st_mode):
        mode = stat.S_IMODE(sourceStat.st_mode) or 0o755
        _addDirectory(tf, destPath, mode, mtime, writtenPaths)
        for entry in sorted(os.listdir(sourcePath)):
            childSource = os.path.join(sourcePath, entry)
            childDest = os.path.join(destPath, entry)
            _addPathRecursive(tf, childSource, childDest, mtime, writtenPaths)
        return

    if stat.S_ISREG(sourceStat.st_mode):
        _addFile(tf, sourcePath, destPath, mtime, writtenPaths)
        return

    raise RuntimeError(f"Unsupported filesystem entry type at {sourcePath}")

def _isSubpath(path: str, maybeAncestor: str) -> bool:
    normPath = os.path.normpath(path)
    normAncestor = os.path.normpath(maybeAncestor)
    if normPath == normAncestor:
        return True
    return normPath.startswith(normAncestor + os.sep)

def _dedupeAbsolutePaths(rawPaths: list[str]) -> list[str]:
    uniquePaths = sorted(dict.fromkeys(rawPaths))
    absolutePaths = [p for p in uniquePaths if os.path.isabs(p)]
    relativePaths = [p for p in uniquePaths if not os.path.isabs(p)]

    # Prefer shallow roots first so deeper duplicates are dropped deterministically.
    absolutePaths.sort(key=lambda p: (len(os.path.normpath(p)), os.path.normpath(p)))

    kept: list[str] = []
    keptCanonicalExisting: list[str] = []

    for path in absolutePaths:
        if any(_isSubpath(path, existingPath) for existingPath in kept):
            continue

        if os.path.lexists(path):
            canonicalPath = os.path.realpath(path)
            if any(_isSubpath(canonicalPath, existingCanonical) for existingCanonical in keptCanonicalExisting):
                continue
            keptCanonicalExisting.append(canonicalPath)

        kept.append(path)

    return sorted(kept + relativePaths)

def main() -> None:
    parser = argparse.ArgumentParser(description="Create deterministic tar from depset path manifest.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--create-missing-dirs", action="store_true", default=False)
    parser.add_argument("--dedupe-absolute-paths", action="store_true", default=False)
    args = parser.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as manifestFile:
        manifest = json.load(manifestFile)

    rawPaths = manifest.get("paths", [])
    if not isinstance(rawPaths, list):
        raise RuntimeError("manifest.paths must be a list")

    if args.dedupe_absolute_paths:
        sortedPaths = _dedupeAbsolutePaths(rawPaths)
    else:
        sortedPaths = sorted(dict.fromkeys(rawPaths))
    mtime = _getMtime()
    writtenPaths: set[str] = set()

    # Use GNU tar format for dpkg compatibility. PAX extended headers (`typeflag x`)
    # are rejected by some dpkg versions when unpacking .deb payloads.
    with tarfile.open(args.output, mode="w", format=tarfile.GNU_FORMAT, dereference=False) as tf:
        for sourcePath in sortedPaths:
            if not isinstance(sourcePath, str):
                raise RuntimeError("All manifest paths must be strings")

            if os.path.lexists(sourcePath):
                _addPathRecursive(tf, sourcePath, sourcePath, mtime, writtenPaths)
                continue

            if args.create_missing_dirs:
                _addDirectory(tf, sourcePath, 0o755, mtime, writtenPaths)
                continue

            print(f"[depset-tar] skipping missing path: {sourcePath}", file=sys.stderr)


if __name__ == "__main__":
    main()
