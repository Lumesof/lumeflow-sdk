# tools/runfiles_tar/pkg_tool.py
# Lightweight, reproducible tar writer with symlink + tree artifact support.
# Requires Python 3.7+.

import argparse
import json
import os
import stat
import tarfile

def _mtime():
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    return int(sde) if sde and sde.isdigit() else 0

def _norm(path: str) -> str:
    return path.lstrip("/")

def _add_file(tf: tarfile.TarFile, src: str, dst: str, mode: int | None, mtime: int) -> None:
    st = os.stat(src)
    size = st.st_size
    if mode is None:
        m = stat.S_IMODE(st.st_mode)
        m |= stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        mode = m
    ti = tarfile.TarInfo(_norm(dst))
    ti.size = size
    ti.mode = mode
    ti.mtime = mtime
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    with open(src, "rb") as f:
        tf.addfile(ti, f)

def _rel_target_for_runfiles(link_name: str, target_under_rf: str) -> str:
    """
    Convert a runfiles-root-relative target (e.g., '_main/x/y') into a path
    relative to the directory of link_name (which lives somewhere under
    '<something>.runfiles/...').
    """
    ln = _norm(link_name)
    link_dir = os.path.dirname(ln)

    key = ".runfiles/"
    i = ln.find(key)
    if i < 0:
        # Not under a *.runfiles/ path; best effort: return as-is.
        return target_under_rf

    rf_root = ln[: i + len(".runfiles")]  # up to and including ".runfiles"
    # Build pseudo-absolute paths so relpath has a common anchor; strip later.
    abs_target = "/" + rf_root + "/" + target_under_rf
    abs_start = "/" + link_dir
    return os.path.relpath(abs_target, start=abs_start)

def _add_symlink(tf: tarfile.TarFile, link_name: str, target: str, mtime: int) -> None:
    # Rewrite runfiles-root-relative target to be relative to the symlink's directory.
    rewritten = _rel_target_for_runfiles(link_name, target)

    ti = tarfile.TarInfo(_norm(link_name))
    ti.type = tarfile.SYMTYPE
    ti.linkname = rewritten
    ti.mode = 0o777
    ti.mtime = mtime
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    tf.addfile(ti)

def _add_directory(tf: tarfile.TarFile, dir_dst: str, mtime: int, mode: int = 0o755) -> None:
    ti = tarfile.TarInfo(_norm(dir_dst.rstrip("/")))
    ti.type = tarfile.DIRTYPE
    ti.mode = mode
    ti.mtime = mtime
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    tf.addfile(ti)

def _walk_tree_and_add(tf: tarfile.TarFile, src_root: str, dst_root: str, mtime: int, keep_empty_dirs: bool) -> None:
    # Deterministic walk order
    for dirpath, dirnames, filenames in os.walk(src_root, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()

        rel_dir = os.path.relpath(dirpath, src_root)
        rel_dir = "" if rel_dir == "." else rel_dir

        # Add files (always dereference symlinks to store file contents)
        for name in filenames:
            src = os.path.join(dirpath, name)
            rel = name if not rel_dir else f"{rel_dir}/{name}"
            dst = f"{dst_root}/{rel}"
            _add_file(tf, src, dst, mode=None, mtime=mtime)

        # Optionally add explicit entries for empty directories
        if keep_empty_dirs and not filenames and not dirnames:
            dst = f"{dst_root}/{rel_dir}" if rel_dir else dst_root
            _add_directory(tf, dst, mtime)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--keep-empty-dirs", action="store_true", default=False,
                    help="Emit explicit entries for empty directories inside tree artifacts.")
    args = ap.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as f:
        man = json.load(f)

    files = list(man.get("files", []))
    files.sort(key=lambda e: _norm(e.get("dst", "")))

    symlinks = list(man.get("symlinks", []))
    symlinks.sort(key=lambda e: _norm(e.get("link_name", "")))

    mtime = _mtime()

    with tarfile.open(args.output, mode="w", format=tarfile.PAX_FORMAT, dereference=False) as tf:
        # Files and directory entries (tree artifacts)
        for ent in files:
            src = ent["src"]
            dst = ent["dst"]

            # Honor manifest flag, but also autodetect real directories (tree artifacts)
            is_dir = ent.get("is_dir", False)
            if not is_dir:
                try:
                    st_mode = os.lstat(src).st_mode
                    is_dir = stat.S_ISDIR(st_mode)
                except FileNotFoundError:
                    is_dir = False

            if is_dir:
                _walk_tree_and_add(tf, src, dst, mtime, keep_empty_dirs=args.keep_empty_dirs)
            else:
                _add_file(tf, src, dst, ent.get("mode"), mtime)

        # Symlinks (from runfiles symlink sets)
        for ent in symlinks:
            _add_symlink(tf, ent["link_name"], ent["target"], mtime)

if __name__ == "__main__":
    main()
