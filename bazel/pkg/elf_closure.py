#!/usr/bin/env python3
# tools/pack_combined_app_and_cc_closure.py
#
# Combine an existing app layer tar (optional) with an ELF runtime closure tar
# computed directly from each object's RUNPATH/RPATH + NEEDED. Produces a single,
# deterministic tar suitable as an OCI layer.
#
# Algorithm (per spec):
# - For each DT_NEEDED SONAME of the current ELF object:
#     * Walk that object's RUNPATH (or RPATH if RUNPATH absent) IN ORDER.
#     * On the first directory D where D/SONAME exists on the dev machine,
#       resolve to its real bytes (realpath) and WRITE A REGULAR FILE at D/SONAME
#       in the tar (NO SYMLINKS). Stop searching for that SONAME.
# - Recurse into each resolved dependency (using the dependent object's own
#   RUNPATH/RPATH). If that list is empty, FALL BACK to the ROOT BINARY'S
#   RUNPATH/RPATH to resolve its deps.
# - Also copy the dynamic loader (PT_INTERP) as a regular file at its absolute path.
#
# Determinism:
# - PAX tar, fixed uid/gid/uname/gname, mtime from SOURCE_DATE_EPOCH (else 0),
#   sorted writes, synth dirs 0755, first-write-wins (skip duplicates).
#
# Usage:
#   python3 tools/pack_combined_app_and_cc_closure.py \
#       --binary /ABS/path/to/app/.bin/server \
#       --output /ABS/path/out.tar \
#       [--base-tar /ABS/path/app_layer.tar]
#
# Notes:
# - Designed for local/unsandboxed Bazel actions where RUNPATH points at absolute
#   CAS mounts. We DO NOT scan subdirectories; we only probe D/SONAME exactly.

import argparse
import io
import os
import re
import stat
import subprocess
import tarfile
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

# -------------------- determinism helpers --------------------

def _mtime() -> int:
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    return int(sde) if sde and sde.isdigit() else 0

def _strip_leading_slash(p: str) -> str:
    return p[1:] if p.startswith("/") else p

def _norm_tar_path(p: str) -> str:
    # Tar entries must be relative and should not contain leading "./"
    q = _strip_leading_slash(p)
    return q.lstrip("./")

def _fix_info_meta(ti: tarfile.TarInfo, mtime: int) -> None:
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    ti.mtime = mtime
    # default sane modes if not set
    if ti.isfile():
        ti.mode = (ti.mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) & 0o777
    elif ti.isdir() and ti.mode == 0:
        ti.mode = 0o755
    elif ti.issym() and ti.mode == 0:
        ti.mode = 0o777

# -------------------- ELF parsing --------------------

def _run(cmd: List[str]) -> str:
    out = subprocess.checkoutput(cmd, stderr=subprocess.STDOUT)  # noqa: N816
    return out.decode("utf-8", "replace")

# keep compatibility with different PEP8 linters:
subprocess.checkoutput = subprocess.check_output  # type: ignore[attr-defined]

def _readelf_dynamic(path: str) -> str:
    return _run(["readelf", "-dW", path])

def _readelf_prog_headers(path: str) -> str:
    return _run(["readelf", "-lW", path])

_SQBR = re.compile(r"\[(.*?)\]")

def _sq(line: str) -> List[str]:
    return _SQBR.findall(line)

def _parse_runpath_rpath(dyn_txt: str) -> Tuple[List[str], List[str]]:
    runpath: List[str] = []
    rpath: List[str] = []
    for line in dyn_txt.splitlines():
        if "RUNPATH" in line:
            vals = _sq(line)
            if vals:
                runpath = [p for p in vals[0].split(":") if p]
        elif "RPATH" in line:
            vals = _sq(line)
            if vals:
                rpath = [p for p in vals[0].split(":") if p]
    return runpath, rpath

def _parse_needed(dyn_txt: str) -> List[str]:
    needs: List[str] = []
    for line in dyn_txt.splitlines():
        if "NEEDED" in line:
            vals = _sq(line)
            if vals:
                needs.append(vals[0].strip())
    return needs

def _parse_soname(dyn_txt: str) -> Optional[str]:
    for line in dyn_txt.splitlines():
        if "SONAME" in line:
            vals = _sq(line)
            if vals:
                return vals[0].strip()
    return None

def _parse_interpreter(ph_txt: str) -> Optional[str]:
    for line in ph_txt.splitlines():
        if "Requesting program interpreter" in line:
            vals = _sq(line)
            if vals:
                parts = vals[0].split(": ", 1)
                if len(parts) == 2:
                    return parts[1].strip()
    return None

def _is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except Exception:
        return False

# -------------------- tar writing helpers --------------------

def _ensure_dirs(tf: tarfile.TarFile, dst_abs: str, mtime: int, existing_paths: Set[str]) -> None:
    """Create DIRTYPE entries (0755) for parent dirs not yet in the tar."""
    parts: List[str] = []
    cur = _norm_tar_path(os.path.dirname(dst_abs))
    while cur and cur not in existing_paths:
        parts.append(cur)
        cur = os.path.dirname(cur)
    for d in reversed(parts):
        ti = tarfile.TarInfo(d.rstrip("/"))
        ti.type = tarfile.DIRTYPE
        ti.mode = 0o755
        _fix_info_meta(ti, mtime)
        tf.addfile(ti)
        existing_paths.add(d)

def _add_file_from_fs(tf: tarfile.TarFile, src: str, dst_abs: str, mtime: int, existing_paths: Set[str]) -> None:
    """Copy bytes from src and write a REGULAR file at dst_abs in the tar."""
    arc = _norm_tar_path(dst_abs)
    if not arc or arc in existing_paths:
        return
    _ensure_dirs(tf, dst_abs, mtime, existing_paths)
    st = os.stat(src)
    ti = tarfile.TarInfo(arc)
    ti.type = tarfile.REGTYPE
    ti.size = st.st_size
    # conservative: ensure executables (loader) work; libs remain readable by all
    ti.mode = 0o755
    _fix_info_meta(ti, mtime)
    with open(src, "rb") as f:
        tf.addfile(ti, f)
    existing_paths.add(arc)

def _copy_base_tar(tf_out: tarfile.TarFile, base_tar: str, mtime: int, existing_paths: Set[str]) -> None:
    with tarfile.open(base_tar, "r:*") as tf_in:
        members = tf_in.getmembers()
        for ti in members:
            arc = _norm_tar_path(ti.name)
            if not arc or arc in existing_paths:
                continue
            new_ti = tarfile.TarInfo(arc)
            new_ti.type = ti.type
            if ti.isfile():
                f = tf_in.extractfile(ti)
                if f is None:
                    raise RuntimeError(f"Failed to read file from base tar: {ti.name}")
                data = f.read()
                new_ti.size = len(data)
                # preserve exec bit if present; ensure world-readable
                mode = (ti.mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) & 0o777
                new_ti.mode = mode
                _fix_info_meta(new_ti, mtime)
                tf_out.addfile(new_ti, io.BytesIO(data))
            elif ti.isdir():
                new_ti.mode = 0o755
                _fix_info_meta(new_ti, mtime)
                tf_out.addfile(new_ti)
            elif ti.issym():
                new_ti.linkname = ti.linkname
                _fix_info_meta(new_ti, mtime)
                tf_out.addfile(new_ti)
            else:
                continue
            existing_paths.add(arc)

def _add_shrc_file(tf: tarfile.TarFile, pkg_dir: str, dirs: List[str], mtime: int, existing_paths: Set[str]) -> None:
    dst_abs = os.path.join(pkg_dir, ".shrc")
    arc = _norm_tar_path(dst_abs)
    print(f"[elf-closure] Adding .shrc file at {arc}")
    if arc in existing_paths:
        print(f"[elf-closure] WARNING: file {arc} already exists, skipping...")
        return
    _ensure_dirs(tf, dst_abs, mtime, existing_paths)
    joined = ":".join(dirs)
    # POSIX sh-compatible export (preserve existing value if set)
    content = f'export LD_LIBRARY_PATH="{joined}"\n'
    data = content.encode("utf-8")
    ti = tarfile.TarInfo(arc)
    ti.type = tarfile.REGTYPE
    ti.size = len(data)
    ti.mode = 0o644
    _fix_info_meta(ti, mtime)
    tf.addfile(ti, io.BytesIO(data))
    existing_paths.add(arc)

# -------------------- closure builder (with root fallback) --------------------

def _search_dirs_for_obj(obj_abs: str) -> List[str]:
    if not _is_elf(obj_abs):
        return []
    dyn = _readelf_dynamic(obj_abs)
    runpath, rpath = _parse_runpath_rpath(dyn)
    return runpath if runpath else rpath

def _needed_for_obj(obj_abs: str) -> Tuple[List[str], Optional[str]]:
    dyn = _readelf_dynamic(obj_abs)
    return _parse_needed(dyn), _parse_soname(dyn)

def _interpreter_for_obj(obj_abs: str) -> Optional[str]:
    ph = _readelf_prog_headers(obj_abs)
    return _parse_interpreter(ph)

def _probe_first_in_search(soname: str, search_dirs: List[str]) -> Optional[Tuple[str, str]]:
    """
    Return (dst_abs, src_real) for the FIRST directory D in search_dirs
    where D/soname exists. dst_abs = D/soname; src_real = realpath(D/soname).
    No subdir scanning; exact match only.
    """
    for d in search_dirs:
        if not d:
            continue
        cand = os.path.join(d, soname)
        try:
            if os.path.lexists(cand):
                rp = os.path.realpath(cand)
                if os.path.exists(rp):
                    return cand, rp
        except Exception:
            pass
    return None

def build_closure_materialized_as_files(binary: str, root_search_dirs: List[str]) -> Dict[str, str]:
    """
    Returns: files_map {dst_abs: src_real} to write into the tar:
      - interpreter (if any) at its absolute path (bytes from realpath)
      - for each SONAME of each visited object: first D/SONAME from that
        object's search list; if empty, FALL BACK to root_search_dirs.
      - Destination is always D/SONAME (REGULAR FILE), bytes from realpath.
      - Recurses into the resolved real file.
    """
    files: Dict[str, str] = {}

    # interpreter for the top-level binary
    interp = _interpreter_for_obj(binary)
    if interp:
        rp = os.path.realpath(interp)
        if not os.path.exists(rp):
            raise SystemExit(f"[elf-closure] FATAL: interpreter not found: {interp}")
        files.setdefault(interp, rp)

    q: deque[str] = deque([binary])
    visited: Set[str] = set()

    while q:
        cur = q.popleft()
        if cur in visited:
            continue
        visited.add(cur)

        needs, _cur_soname = _needed_for_obj(cur)
        search_dirs = _search_dirs_for_obj(cur)
        if not search_dirs:
            search_dirs = root_search_dirs  # <— fallback per request

        for n in needs:
            hit = _probe_first_in_search(n, search_dirs)
            if not hit:
                raise SystemExit(f"[elf-closure] FATAL: could not resolve {n} via RUNPATH/RPATH for {cur}")
            dst_abs, src_real = hit

            # Record destination path as EXACT D/SONAME, with bytes from src_real
            files.setdefault(dst_abs, src_real)

            # Recurse into the resolved real file
            q.append(src_real)

    return files

# -------------------- main --------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Combine base app tar and ELF runtime closure (as regular files) into one deterministic tar.")
    ap.add_argument("--binary", required=True, help="Absolute path to the app's ELF binary (in the image layout).")
    ap.add_argument("--pkg-dir", required=True, help="Absolute path to the app's package directory (in the image layout).")
    ap.add_argument("--output", required=True, help="Path to write the combined .tar")
    ap.add_argument("--base-tar", help="Optional base app tar to include first.")
    # NEW FLAGS (minimal, surgical additions):
    ap.add_argument("--extra-soname", action="append", default=[],
                    help="Additional SONAME to include (repeatable). Resolved via the root binary's RUNPATH/RPATH, then closed transitively.")
    ap.add_argument("--strict-extra", action="store_true",
                    help="Fail if any --extra-soname cannot be resolved (default: warn and continue).")
    args = ap.parse_args()

    bin_path = os.path.realpath(args.binary)
    if not os.path.isabs(bin_path):
        raise SystemExit("--binary must be an absolute path")
    if not os.path.exists(bin_path):
        raise SystemExit(f"Binary does not exist: {bin_path}")
    
    pkg_dir = args.pkg_dir

    mtime = _mtime()
    existing_paths: Set[str] = set()

    # Compute root search dirs once (used for fallback)
    root_search_dirs = _search_dirs_for_obj(bin_path)

    print(f"Creating {args.output}")
    with tarfile.open(args.output, mode="w", format=tarfile.PAX_FORMAT, dereference=False) as tf_out:
        # 1) Copy base tar first (if any)
        if args.base_tar:
            _copy_base_tar(tf_out, args.base_tar, mtime, existing_paths)

        # 2) If not ELF, done (base-only or empty tar)
        if not _is_elf(bin_path):
            return

        # 3) Build closure materialized strictly as files at D/SONAME (with fallback)
        files_map = build_closure_materialized_as_files(bin_path, root_search_dirs)

        # 3b) Resolve --extra-soname items via the root search list and merge their closures
        if args.extra_soname:
            # de-dup while preserving CLI order
            for soname in dict.fromkeys(args.extra_soname):
                hit = _probe_first_in_search(soname, root_search_dirs)
                print(f"[elf-closure] INFO: looking up {soname}")
                if not hit:
                    msg = f"[elf-closure] WARNING: --extra-soname {soname} not found via root RUNPATH/RPATH"
                    if args.strict_extra:
                        raise SystemExit(msg.replace("WARNING", "FATAL"))
                    else:
                        print(msg)
                        continue
                dst_abs, src_real = hit
                files_map.setdefault(dst_abs, src_real)
                # include its transitive deps using the same rules
                extra_map = build_closure_materialized_as_files(src_real, root_search_dirs)
                files_map.update(extra_map)

            # --- add .shrc so launcher can set LD_LIBRARY_PATH when extras are used ---
            appdir = os.path.dirname(os.path.dirname(bin_path))  # /app from /app/.bin/<exe>
            # de-dup root dirs while preserving order
            seen = set()
            ordered = []
            for d in root_search_dirs:
                if d and d not in seen:
                    seen.add(d)
                    ordered.append(d)
            _add_shrc_file(tf_out, pkg_dir, ordered, mtime, existing_paths)

        # 4) Write files, skipping any that already exist from base tar (sorted for determinism)
        for dst_abs, src_real in sorted(files_map.items(), key=lambda kv: _norm_tar_path(kv[0])):
            _add_file_from_fs(tf_out, src_real, dst_abs, mtime, existing_paths)

        # Friendly log: print the top-level binary's search order
        try:
            runpath, rpath = _parse_runpath_rpath(_readelf_dynamic(bin_path))
            print("[elf-closure] RUNPATH/RPATH search order for binary:")
            for d in (runpath if runpath else rpath):
                print(f"  - {d}")
        except Exception:
            pass

if __name__ == "__main__":
    main()
