#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app_image_tool: expand a deterministic package tar into a true AppImage artifact.

Inputs:
  --input-tar: path to the tar from `binary_tar`
  --output:    path to write the resulting .appimage
  --app-name:  human-readable app name (also used for .desktop and icon base)
  --arch:      x86_64 | aarch64 (metadata only)
  --package-dir: where the app is laid out inside the tar (e.g., "/app")
  --compression: zstd | xz   (hint to appimagetool via APPIMAGE_COMPRESSION)
  --source-date-epoch: integer epoch; sets all mtimes and is passed to appimagetool
  --version:   string version embedded in the .desktop (optional)
  --icon:      optional icon file (.png/.svg/.ico)
  --desktop:   optional .desktop file to use verbatim
  --desktop-template: optional template; placeholders: {Name} {Exec} {Icon} {Version} {Arch}
  --appimage-tool-path: absolute path to the `appimagetool` binary to invoke

Reproducibility:
  • All file mtimes are normalized to SOURCE_DATE_EPOCH (or 0 if unset).
  • appimagetool is invoked with SOURCE_DATE_EPOCH in the environment.
  • Overlay into AppDir is done in lexicographic order for stable layout.

Requirement:
  • The path provided via --appimage-tool-path must exist and be executable.
"""

import argparse
import base64
import os
import sys
import tarfile
import tempfile
import shutil
import subprocess
from pathlib import Path


# ---------------------------
# Argument parsing (public)
# ---------------------------
def parseArgs():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-tar", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--arch", choices=["x86_64", "aarch64"], default="x86_64")
    parser.add_argument("--package-dir", default="/app")
    parser.add_argument("--compression", choices=["zstd", "xz"], default="zstd")
    parser.add_argument("--source-date-epoch", default="0")
    parser.add_argument("--version", default="")
    parser.add_argument("--icon")
    parser.add_argument("--desktop")
    parser.add_argument("--desktop-template")
    parser.add_argument("--appimage-tool-path", required=True)

    # Legacy flags kept for compatibility (no-ops with appimagetool)
    parser.add_argument("--squashfs-no-xattrs", action="store_true")
    parser.add_argument("--squashfs-no-append", action="store_true")
    parser.add_argument("--squashfs-sort", action="store_true")
    parser.add_argument("--uniform-owners", action="store_true")

    return parser.parse_args()


# ---------------------------
# Tooling helpers (private)
# ---------------------------
def _isExecFile(path: Path) -> bool:
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    return path.is_file() and os.access(str(path), os.X_OK)

# ---------------------------
# Icon helpers (private)
# ---------------------------
def _writeBytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

# 1x1 transparent PNG placeholder (base64)
_PLACEHOLDER_PNG_B64 = (
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
    b"ASsJTYQAAAAASUVORK5CYII="
)

def _ensureIcon(app_dir: Path, app_name: str, provided_icon_path):
    """Copy provided icon to <AppDir>/<app_name>.<ext>, or synthesize a tiny PNG."""
    if provided_icon_path:
        src_icon = Path(provided_icon_path)
        ext = src_icon.suffix.lower() or ".png"
        _copyFile(src_icon, app_dir / f"{app_name}{ext}", executable=False)
        return
    # No icon provided → write placeholder PNG as <app_name>.png
    png_bytes = base64.b64decode(_PLACEHOLDER_PNG_B64)
    _writeBytes(app_dir / f"{app_name}.png", png_bytes)

# ---------------------------
# FS & tar helpers (private)
# ---------------------------
def _extractTar(tar_path: Path, dest: Path):
    with tarfile.open(tar_path, "r:*") as tf:
        _safeExtract(tf, path=dest)


def _safeExtract(tar: tarfile.TarFile, path: Path):
    base = path.resolve()
    for member in tar.getmembers():
        member_path = (base / member.name).resolve()
        if not str(member_path).startswith(str(base)):
            raise Exception(f"Tar contains path traversal: {member.name}")
    tar.extractall(path=base)


def _writeFile(path: Path, data: str, executable: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    if executable:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | 0o111)


def _copyFile(src: Path, dst: Path, executable: bool | None = None):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if executable is not None:
        mode = os.stat(dst).st_mode
        os.chmod(dst, (mode | 0o111) if executable else (mode & ~0o111))


def _synthesizeDesktop(app_name: str, exec_rel: str, icon_base: str, version: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={app_name}\n"
        f"Exec={exec_rel} %U\n"
        f"Icon={icon_base}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        f"X-AppImage-Version={version}\n"
    )


def _applyTemplate(template_text: str, **kwargs) -> str:
    return template_text.format(**kwargs)


def _makeAppRun(app_dir: Path, package_rel: str):
    body = f"""#!/usr/bin/env sh
set -e
HERE="${{APPDIR:-$(cd "$(dirname "$0")" && pwd)}}"
exec "$HERE/{package_rel}/run" "$@"
"""
    _writeFile(app_dir / "AppRun", body, executable=True)


def _setTreeMtime(root: Path, epoch: int):
    for p in sorted(root.rglob("*")):
        try:
            os.utime(p, (epoch, epoch), follow_symlinks=False)
        except Exception:
            pass
    try:
        os.utime(root, (epoch, epoch))
    except Exception:
        pass


# ---------------------------
# Build step (private)
# ---------------------------
def _buildWithAppImageTool(appimage_tool_path: Path, app_dir: Path, out_path: Path,
                           epoch: int, compression: str, arch: str,
                           work_tmp_dir: Path):
    env = dict(os.environ)
    # Avoid SDE conflict with mksquashfs -mkfs-time:
    env.pop("SOURCE_DATE_EPOCH", None)
    env["APPIMAGE_COMPRESSION"] = "ZSTD" if compression.lower() == "zstd" else "XZ"
    # Prefer normal AppImage execution (FUSE mount path) to avoid shared
    # extract-and-run temp directory collisions across parallel actions.
    # Keep appimagetool caches in a writable, per-invocation location.
    cache_home = work_tmp_dir / "cache-home"
    cache_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(cache_home)
    env["XDG_CACHE_HOME"] = str(cache_home / ".cache")
    env["APPIMAGE_CACHE_DIR"] = str(cache_home / ".cache" / "appimage")
    # >>> Tell appimagetool which arch to use when multiple are present:
    env["ARCH"] = arch

    cmd = [str(appimage_tool_path), "-n", str(app_dir), str(out_path)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    if proc.returncode != 0:
        sys.stderr.write("[app_image_tool] appimagetool failed:\n" + proc.stdout + "\n")
        sys.exit(proc.returncode)


# ---------------------------
# Main
# ---------------------------
def runMain():
    args = parseArgs()

    appimage_tool_path = Path(args.appimage_tool_path)
    if not _isExecFile(appimage_tool_path):
        sys.stderr.write(f"[app_image_tool] --appimage-tool-path is not an executable file: {appimage_tool_path}\n")
        sys.exit(2)

    sde_str = args.source_date_epoch or "0"
    try:
        epoch = int(sde_str)
    except ValueError:
        sys.stderr.write(f"[app_image_tool] --source-date-epoch must be an integer: {sde_str}\n")
        sys.exit(2)

    input_tar = Path(args.input_tar).resolve()
    output_img = Path(args.output).resolve()
    app_name = args.app_name
    package_dir = args.package_dir.strip() or "/app"
    package_rel = package_dir.lstrip("/")

    with tempfile.TemporaryDirectory(prefix="app_image_tool_") as tmp:
        tmp_path = Path(tmp)
        extract_root = tmp_path / "extract"
        app_dir = tmp_path / "AppDir"
        extract_root.mkdir(parents=True, exist_ok=True)
        app_dir.mkdir(parents=True, exist_ok=True)

        # 1) Extract tar into extract_root
        _extractTar(input_tar, extract_root)

        # 2) Overlay into AppDir deterministically
        for item in sorted(extract_root.iterdir(), key=lambda p: p.name):
            target = app_dir / item.name
            if target.exists():
                if item.is_dir():
                    for sub in sorted(item.rglob("*")):
                        rel = sub.relative_to(item)
                        dest = target / rel
                        if sub.is_dir():
                            dest.mkdir(parents=True, exist_ok=True)
                        elif sub.is_symlink():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                if dest.exists() or dest.is_symlink():
                                    dest.unlink()
                            except Exception:
                                pass
                            os.symlink(os.readlink(sub), dest)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(sub, dest)
                else:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                    shutil.copy2(item, target)
            else:
                try:
                    item.rename(target)
                except OSError:
                    if item.is_dir():
                        shutil.copytree(item, target, symlinks=True)
                    else:
                        shutil.copy2(item, target)

        # 3) AppRun
        _makeAppRun(app_dir, package_rel=package_rel)

        # 4) .desktop
        desktop_target = app_dir / f"{app_name}.desktop"
        if args.desktop:
            _copyFile(Path(args.desktop), desktop_target, executable=False)
        elif args.desktop_template:
            tmpl = Path(args.desktop_template).read_text(encoding="utf-8")
            rendered = _applyTemplate(
                tmpl,
                Name=app_name,
                Exec="./AppRun",
                Icon=app_name,
                Version=args.version or "",
                Arch=args.arch,
            )
            _writeFile(desktop_target, rendered, executable=False)
        else:
            rendered = _synthesizeDesktop(
                app_name=app_name,
                exec_rel="./AppRun",
                icon_base=app_name,
                version=args.version or "",
            )
            _writeFile(desktop_target, rendered, executable=False)

        # 5) Icon: copy provided one or synthesize a placeholder so appimagetool is happy
        _ensureIcon(app_dir, app_name, args.icon)

        # 6) Normalize mtimes for reproducibility
        _setTreeMtime(app_dir, epoch)

        # 7) Build final AppImage with the provided tool path
        output_img.parent.mkdir(parents=True, exist_ok=True)
        _buildWithAppImageTool(
            appimage_tool_path=appimage_tool_path,
            app_dir=app_dir,
            out_path=output_img,
            epoch=epoch,
            compression=args.compression,
            arch=args.arch,
            work_tmp_dir=tmp_path,
        )

    # Make sure the produced AppImage is executable for `bazel run`
    try:
        st_mode = os.stat(output_img).st_mode
        os.chmod(output_img, st_mode | 0o111)
    except Exception:
        pass

    # Normalize outer file mtime for “archives of archives”
    try:
        os.utime(output_img, (epoch, epoch))
    except Exception:
        pass

    print(f"[app_image_tool] Wrote deterministic AppImage: {output_img}")

if __name__ == "__main__":
    runMain()
