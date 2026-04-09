#############################################
# Helpers
#############################################
load("//bazel/archive_paths:archive_paths_info.bzl", "lumesof_archive_paths_info")

def _join(*parts):
    """Helper function to join paths properly."""
    cleaned = [p.strip("/") for p in parts if p]
    return "/" + "/".join(cleaned)

def _assert_rel(name, what):
    """Ensure that the name is a relative path."""
    if type(name) != "string" or not name:
        fail("{} must be a non-empty relative path inside the CAS dir, got: {}".format(what, name))
    if name.startswith("/"):
        fail("{} must be relative (no leading '/'), got: {}".format(what, name))

#############################################
# Provider: absolute paths to tools
#############################################

SystemToolchainInfo = provider(
    doc = "Absolute paths to system tools resolved from <cas_root>/<sha256>/<tool_relative_name>.",
    fields = {
        # AppImage + desktop metadata
        "appimagetool": "Absolute path to 'appimagetool'.",
        "appstreamcli": "Absolute path to 'appstreamcli'.",

        # Core UNIX utils and compression
        "awk": "Absolute path to 'awk'.",
        "curl": "Absolute path to 'curl'.",
        "file": "Absolute path to 'file'.",
        "grep": "Absolute path to 'grep'.",
        "gzip": "Absolute path to 'gzip'.",
        "sed": "Absolute path to 'sed'.",
        "sha256sum": "Absolute path to 'sha256sum'.",
        "tar": "Absolute path to 'tar'.",
        "unzip": "Absolute path to 'unzip'.",
        "wget": "Absolute path to 'wget'.",
        "xz": "Absolute path to 'xz'.",
        "zip": "Absolute path to 'zip'.",
        "zstd": "Absolute path to 'zstd'.",

        # ELF & image helpers
        "ldd": "Absolute path to 'ldd'.",
        "mksquashfs": "Absolute path to 'mksquashfs'.",
        "patchelf": "Absolute path to 'patchelf'.",
        "readelf": "Absolute path to 'readelf'.",
        "strip": "Absolute path to 'strip'.",
    },
)

#############################################
# Rule implementation
#############################################

def _system_toolchain_impl(ctx):
    cas_dir = _join(ctx.attr.cas_root, ctx.attr.sha256)

    # Validate relative names (Starlark-safe checks)
    _assert_rel(ctx.attr.appimagetool_name, "appimagetool_name")
    _assert_rel(ctx.attr.appstreamcli_name, "appstreamcli_name")

    _assert_rel(ctx.attr.awk_name, "awk_name")
    _assert_rel(ctx.attr.curl_name, "curl_name")
    _assert_rel(ctx.attr.file_name, "file_name")
    _assert_rel(ctx.attr.grep_name, "grep_name")
    _assert_rel(ctx.attr.gzip_name, "gzip_name")
    _assert_rel(ctx.attr.sed_name, "sed_name")
    _assert_rel(ctx.attr.sha256sum_name, "sha256sum_name")
    _assert_rel(ctx.attr.tar_name, "tar_name")
    _assert_rel(ctx.attr.unzip_name, "unzip_name")
    _assert_rel(ctx.attr.wget_name, "wget_name")
    _assert_rel(ctx.attr.xz_name, "xz_name")
    _assert_rel(ctx.attr.zip_name, "zip_name")
    _assert_rel(ctx.attr.zstd_name, "zstd_name")

    _assert_rel(ctx.attr.ldd_name, "ldd_name")
    _assert_rel(ctx.attr.mksquashfs_name, "mksquashfs_name")
    _assert_rel(ctx.attr.patchelf_name, "patchelf_name")
    _assert_rel(ctx.attr.readelf_name, "readelf_name")
    _assert_rel(ctx.attr.strip_name, "strip_name")

    return [platform_common.ToolchainInfo(
        systeminfo = SystemToolchainInfo(
            appimagetool = _join(cas_dir, ctx.attr.appimagetool_name),
            appstreamcli = _join(cas_dir, ctx.attr.appstreamcli_name),

            awk = _join(cas_dir, ctx.attr.awk_name),
            curl = _join(cas_dir, ctx.attr.curl_name),
            file = _join(cas_dir, ctx.attr.file_name),
            grep = _join(cas_dir, ctx.attr.grep_name),
            gzip = _join(cas_dir, ctx.attr.gzip_name),
            sed = _join(cas_dir, ctx.attr.sed_name),
            sha256sum = _join(cas_dir, ctx.attr.sha256sum_name),
            tar = _join(cas_dir, ctx.attr.tar_name),
            unzip = _join(cas_dir, ctx.attr.unzip_name),
            wget = _join(cas_dir, ctx.attr.wget_name),
            xz = _join(cas_dir, ctx.attr.xz_name),
            zip = _join(cas_dir, ctx.attr.zip_name),
            zstd = _join(cas_dir, ctx.attr.zstd_name),

            ldd = _join(cas_dir, ctx.attr.ldd_name),
            mksquashfs = _join(cas_dir, ctx.attr.mksquashfs_name),
            patchelf = _join(cas_dir, ctx.attr.patchelf_name),
            readelf = _join(cas_dir, ctx.attr.readelf_name),
            strip = _join(cas_dir, ctx.attr.strip_name),
        ),
        cas_dir = cas_dir,  # optional convenience
    )]

#############################################
# Public rule
#############################################

system_toolchain = rule(
    implementation = _system_toolchain_impl,
    attrs = {
        "cas_root": attr.string(
            default = "/lumesof/build-infra/ro-repos/repo-cas",
            doc = "Root of the Content-Addressable Store (CAS).",
        ),
        "sha256": attr.string(
            mandatory = True,
            doc = "SHA256 of the mounted CAS directory that contains the tools.",
        ),

        # Relative paths within CAS (default to tool names)
        "appimagetool_name": attr.string(default = "appimagetool"),
        "appstreamcli_name": attr.string(default = "appstreamcli"),

        "awk_name": attr.string(default = "awk"),
        "curl_name": attr.string(default = "curl"),
        "file_name": attr.string(default = "file"),
        "grep_name": attr.string(default = "grep"),
        "gzip_name": attr.string(default = "gzip"),
        "sed_name": attr.string(default = "sed"),
        "sha256sum_name": attr.string(default = "sha256sum"),
        "tar_name": attr.string(default = "tar"),
        "unzip_name": attr.string(default = "unzip"),
        "wget_name": attr.string(default = "wget"),
        "xz_name": attr.string(default = "xz"),
        "zip_name": attr.string(default = "zip"),
        "zstd_name": attr.string(default = "zstd"),

        "ldd_name": attr.string(default = "ldd"),
        "mksquashfs_name": attr.string(default = "mksquashfs"),
        "patchelf_name": attr.string(default = "patchelf"),
        "readelf_name": attr.string(default = "readelf"),
        "strip_name": attr.string(default = "strip"),
    },
    doc = """Resolves absolute tool paths from <cas_root>/<sha256>/<tool_relative_name>.
             All *_name values must be relative (no leading '/'). The provider SystemToolchainInfo
             exposes absolute paths for the listed tools.""",
)

def system_archive_paths(
        name,
        sha256,
        cas_root = "/lumesof/build-infra/ro-repos/repo-cas",
        appimagetool_name = "appimagetool",
        appstreamcli_name = "appstreamcli",
        awk_name = "awk",
        curl_name = "curl",
        file_name = "file",
        grep_name = "grep",
        gzip_name = "gzip",
        sed_name = "sed",
        sha256sum_name = "sha256sum",
        tar_name = "tar",
        unzip_name = "unzip",
        wget_name = "wget",
        xz_name = "xz",
        zip_name = "zip",
        zstd_name = "zstd",
        ldd_name = "ldd",
        mksquashfs_name = "mksquashfs",
        patchelf_name = "patchelf",
        readelf_name = "readelf",
        strip_name = "strip",
        visibility = None):
    """Expose host tool binaries as archive paths metadata."""
    _assert_rel(appimagetool_name, "appimagetool_name")
    _assert_rel(appstreamcli_name, "appstreamcli_name")
    _assert_rel(awk_name, "awk_name")
    _assert_rel(curl_name, "curl_name")
    _assert_rel(file_name, "file_name")
    _assert_rel(grep_name, "grep_name")
    _assert_rel(gzip_name, "gzip_name")
    _assert_rel(sed_name, "sed_name")
    _assert_rel(sha256sum_name, "sha256sum_name")
    _assert_rel(tar_name, "tar_name")
    _assert_rel(unzip_name, "unzip_name")
    _assert_rel(wget_name, "wget_name")
    _assert_rel(xz_name, "xz_name")
    _assert_rel(zip_name, "zip_name")
    _assert_rel(zstd_name, "zstd_name")
    _assert_rel(ldd_name, "ldd_name")
    _assert_rel(mksquashfs_name, "mksquashfs_name")
    _assert_rel(patchelf_name, "patchelf_name")
    _assert_rel(readelf_name, "readelf_name")
    _assert_rel(strip_name, "strip_name")

    cas_dir = _join(cas_root, sha256)
    lumesof_archive_paths_info(
        name = name,
        archive_paths = [
            _join(cas_dir, appimagetool_name),
            _join(cas_dir, appstreamcli_name),
            _join(cas_dir, awk_name),
            _join(cas_dir, curl_name),
            _join(cas_dir, file_name),
            _join(cas_dir, grep_name),
            _join(cas_dir, gzip_name),
            _join(cas_dir, sed_name),
            _join(cas_dir, sha256sum_name),
            _join(cas_dir, tar_name),
            _join(cas_dir, unzip_name),
            _join(cas_dir, wget_name),
            _join(cas_dir, xz_name),
            _join(cas_dir, zip_name),
            _join(cas_dir, zstd_name),
            _join(cas_dir, ldd_name),
            _join(cas_dir, mksquashfs_name),
            _join(cas_dir, patchelf_name),
            _join(cas_dir, readelf_name),
            _join(cas_dir, strip_name),
        ],
        visibility = visibility,
    )
