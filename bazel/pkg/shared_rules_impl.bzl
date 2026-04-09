"""
Shared packaging implementation used by both public customer-facing rules and
internal Lumesof wrappers.

This file contains only overlap functionality (OCI/AppImage + shared helpers).

WHAT THIS FILE PROVIDES (current & planned)
- `binary_tar` (rule): Packages an executable target’s `DefaultInfo` + runfiles
  (files, symlinks, *root_symlinks*, and tree artifacts) into a reproducible
  `.tar`. Writes a fixed launcher at `<pkg_dir>/<launch_script_name>` that execs
  the Bazel-generated stub under `<pkg_dir>/.bin/<exe>`. Supports preserving
  empty directories from tree artifacts via `keep_empty_dirs`.
- **OCI path**:
- `lumesof_oci_image` (macro): Layers the tar onto a base image via
    `rules_oci.oci_image`, sets entrypoint through `tini`, configures
    user/workdir, and optionally forwards OCI config labels. Forwards
    `keep_empty_dirs` to `binary_tar`.
  - `lumesof_oci_image_publish` (macro): Publishes to Artifact Registry with
    standardized tags derived from the build environment.
- **AppImage path (coming soon)**:
  - Planned macros/rules will reuse `binary_tar` outputs and produce self-contained
    AppImage artifacts with equivalent launcher semantics.

ASSUMPTIONS & CONVENTIONS
- Default layout: `package_dir="/app"`; launcher named `run`; non-root `appuser`;
  `tini` at `/usr/bin/tini` for OCI entrypoints.
- Runfiles symlinks are handled uniformly; if a regular symlink conflicts with a
  root symlink, the *root symlink wins*.
- Tree artifacts in runfiles are recursively added and preserve internal symlinks.
  Empty directories can be kept by passing `--keep-empty-dirs` to the pack tool
  (exposed as `keep_empty_dirs` on `binary_tar` and `lumesof_oci_image`).
- Determinism: sorted entries, fixed uid/gid/uname/gname, uniform mtime
  (`SOURCE_DATE_EPOCH` if set, else 0), PAX tar format.

DEPENDENCIES
- `@rules_oci//oci:defs.bzl` for OCI (`oci_image`, `oci_push`).
- `@bazel_skylib//lib:json.bzl` for manifest encoding.
- Internal tar tool: `//bazel/pkg:pkg_tool` (py_binary) that writes the
  layer and rewrites runfiles symlink targets so they resolve correctly.

QUICK START (OCI)
  load("//path/to:this_file.bzl", "lumesof_oci_image", "lumesof_oci_image_publish")

  lumesof_oci_image(
      name = "vm_waker_image",
      binary = "//services/vm_waker:server",
      package_dir = "/app",
      base = "@lumesof_base_image",
  )

  lumesof_oci_image_publish(
      name = "vm_waker_image_push",
      image = ":vm_waker_image",
  )

PUBLIC MACROS (so far)
- lumesof_oci_image(name, binary, package_dir="/app", base="@lumesof_base_image", keep_empty_dirs=False, labels=None)
- lumesof_oci_image_publish(name, lumesof_pkg, **kwargs)
- lumesof_app_image(name, binary, package_dir="/app", keep_empty_dirs=False, ...)
"""

load("@rules_multirun//:defs.bzl", "command", "multirun")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load", "oci_push")
load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")

def _rf_path(file, workspace_name):
    """Runfiles path for a File under the runfiles root."""
    sp = file.short_path
    if sp.startswith("external/"):
        # Strip "external/" so we get "<repo_name>/..."
        return sp[len("external/"):]
    return "%s/%s" % (workspace_name, sp)

def _normalize_pkg_dir(pkg_dir):
    # Tar entries should be relative (no leading slash)
    d = pkg_dir.strip()
    return d.lstrip("/") if d else "app"

def _binary_tar_impl(ctx):
    bin_tgt = ctx.attr.binary
    if DefaultInfo not in bin_tgt:
        fail("binary must be a build target exposing DefaultInfo (not a single file).")
    di = bin_tgt[DefaultInfo]
    if not di.files_to_run or not di.files_to_run.executable:
        fail("binary must be an executable target (files_to_run.executable missing).")

    exe = di.files_to_run.executable
    pkg_dir_norm = _normalize_pkg_dir(ctx.attr.pkg_dir)
    workspace_name = ctx.workspace_name  # name of the main workspace

    # Merge default_runfiles + data_runfiles (if present).
    rfs = []
    if di.default_runfiles:
        rfs.append(di.default_runfiles)
    if di.data_runfiles:
        rfs.append(di.data_runfiles)

    merged = ctx.runfiles().merge_all(rfs)

    exe_base = exe.basename
    runfiles_root = "%s/.bin/%s.runfiles" % (pkg_dir_norm, exe_base)

    # Collect manifest entries.
    files = []

    # We'll build symlinks in a map so we can de-dup and let root_symlinks win.
    # Map: link_name -> target
    symlink_map = {}

    # 1) The real executable (make sure it's executable in the tar).
    launcher_dst = "%s/.bin/%s" % (pkg_dir_norm, exe_base)
    files.append(struct(
        src = exe,
        dst = launcher_dst,
        mode = 0o755,
    ))

    # 2) Fixed-name launcher stub (single-line body; shebang + one exec line).
    app_launcher = ctx.actions.declare_file(ctx.label.name + "_app_launcher.sh")
    launcher_script = """#!/usr/bin/env sh
        APPDIR="$(cd "$(dirname "$0")" && pwd)"
        export RUNFILES_ROOT="${APPDIR}/.bin/%s.runfiles"
        export RUNFILES_DIR="${APPDIR}/.bin/%s.runfiles"
        if [ -f "${APPDIR}/.shrc" ]; then
            . "${APPDIR}/.shrc"
        fi
        exec "${APPDIR}"/.bin/%s "$@"
        """ % (exe_base, exe_base, exe_base)

    ctx.actions.write(
        output = app_launcher,
        content = launcher_script,
        is_executable = True,
    )
    files.append(struct(
        src = app_launcher,
        dst = "%s/%s" % (pkg_dir_norm, ctx.attr.launch_script_name),
        mode = 0o755,
    ))

    inputs = {exe: None, app_launcher: None}

    # 3) Runfiles (files + symlinks + root_symlinks) under runfiles_root
    if merged:
        # Files
        for f in merged.files.to_list():
            dst_path = "%s/%s" % (runfiles_root, _rf_path(f, workspace_name))
            if getattr(f, "is_directory", False):
                # Tree artifact: record as a directory root; the tool will recurse it.
                files.append(struct(
                    src = f,
                    dst = dst_path,
                    is_dir = True,
                ))
            else:
                files.append(struct(
                    src = f,
                    dst = dst_path,
                ))
            inputs[f] = None

        # Regular symlinks first (lower precedence)
        for e in merged.symlinks.to_list():
            link_name = "%s/%s" % (runfiles_root, e.path)
            target = _rf_path(e.target_file, workspace_name)
            symlink_map[link_name] = target
            inputs[e.target_file] = None

        # Root symlinks next (HIGHER precedence -> overwrite on conflicts)
        for e in merged.root_symlinks.to_list():
            link_name = "%s/%s" % (runfiles_root, e.path)
            target = _rf_path(e.target_file, workspace_name)
            symlink_map[link_name] = target  # overwrites on conflicts
            inputs[e.target_file] = None

    # ---- Deterministic sorting ----
    # Sort files by destination path
    files = sorted(files, key = lambda f: f.dst)

    # Materialize sorted symlinks from the map (root_symlinks already won on conflicts)
    symlinks = [
        struct(link_name = k, target = symlink_map[k])
        for k in sorted(symlink_map.keys())
    ]

    # Write a JSON manifest the tool will consume.
    manifest = ctx.actions.declare_file(ctx.label.name + "_manifest.json")
    manifest_content = struct(
        files = [{"src": f.src.path, "dst": f.dst, "mode": getattr(f, "mode", None)} for f in files],
        symlinks = [{"link_name": s.link_name, "target": s.target} for s in symlinks],
    )
    ctx.actions.write(
        output = manifest,
        content = json.encode(manifest_content),
    )
    inputs[manifest] = None

    # Output tarball.
    tar_out = ctx.actions.declare_file(ctx.label.name + ".tar")

    # Build arguments (conditionally add --keep-empty-dirs)
    args = [
        "--manifest",
        manifest.path,
        "--output",
        tar_out.path,
    ]
    if ctx.attr.keep_empty_dirs:
        args.append("--keep-empty-dirs")

    ctx.actions.run(
        executable = ctx.executable._tar_tool,
        arguments = args,
        # Only data inputs we actually read at runtime:
        inputs = list(inputs.keys()),  # manifest + exe + launcher + data files
        tools = [ctx.executable._tar_tool],  # bring the tool's runfiles/symlinks
        outputs = [tar_out],
        mnemonic = "MakeRunfilesTar",
        progress_message = "Packaging %s into %s" % (ctx.attr.binary.label, tar_out.basename),
    )

    return [DefaultInfo(files = depset([tar_out]))]

binary_tar = rule(
    implementation = _binary_tar_impl,
    attrs = {
        "binary": attr.label(
            mandatory = True,
            executable = True,
            cfg = "target",
        ),
        "pkg_dir": attr.string(default = "/app"),
        "launch_script_name": attr.string(default = "run"),
        "keep_empty_dirs": attr.bool(
            default = False,
            doc = "If True, preserve empty directories from tree artifacts inside the tar.",
        ),
        "_tar_tool": attr.label(
            default = Label("//bazel/pkg:pkg_tool"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = """Packages a binary and its runfiles (incl. root_symlinks) into a tar, with a fixed launcher at <pkg_dir>/<launch_script_name> (default: /app/run). If a symlink conflicts with a root_symlink, the root_symlink wins. Set keep_empty_dirs=True to preserve empty directories from tree artifacts.""",
)


def _add_elf_closure_impl(ctx):
    di = ctx.attr.binary[DefaultInfo]
    if not di.files_to_run or not di.files_to_run.executable:
        fail("binary must be an executable target (files_to_run.executable missing).")

    exe = di.files_to_run.executable
    out_tar = ctx.actions.declare_file(ctx.label.name + ".tar")

    args = [
        "--binary",
        exe.path,  # absolute path to built ELF in action
        "--pkg-dir",
        _normalize_pkg_dir(ctx.attr.pkg_dir),
        "--output",
        out_tar.path,
        "--strict-extra",
    ]

    # Pass through any extra SONAMEs (repeatable flag)
    for s in ctx.attr.extra_sonames:
        args += ["--extra-soname", s]

    inputs = [exe]

    if ctx.file.base_tar:
        args += ["--base-tar", ctx.file.base_tar.path]
        inputs.append(ctx.file.base_tar)

    ctx.actions.run(
        executable = ctx.executable._elf_tool,
        arguments = args,
        inputs = inputs,
        tools = [ctx.executable._elf_tool],
        outputs = [out_tar],
        use_default_shell_env = True,
        mnemonic = "AddElfClosure",
        progress_message = "Packing ELF runtime closure into %s" % out_tar.basename,
    )

    return [DefaultInfo(files = depset([out_tar]))]

_add_elf_closure = rule(
    implementation = _add_elf_closure_impl,
    attrs = {
        "binary": attr.label(
            mandatory = True,
            executable = True,
            cfg = "target",
            doc = "Executable target whose ELF is inspected for PT_INTERP/RUNPATH/NEEDED.",
        ),
        "pkg_dir": attr.string(default = "/app"),
        "base_tar": attr.label(
            allow_single_file = [".tar"],
            doc = "Optional app tar to include first (e.g., from binary_tar). If omitted, produces a tar containing only the ELF closure (or empty for non-ELF).",
        ),
        "extra_sonames": attr.string_list(
            default = [],
            doc = "Additional SONAMEs (dlopen'ed libs like libnss_*.so.2, libresolv.so.2) to resolve via the root binary's RUNPATH/RPATH and include transitively.",
        ),
        "_elf_tool": attr.label(
            default = Label("//bazel/pkg:elf_closure"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = """Create a single tar that contains the app (optional base tar) plus the ELF runtime closure.
            - Inspects the given 'binary' ELF for PT_INTERP and RUNPATH/RPATH, resolves all DT_NEEDED SONAMEs,
              and copies the loader and .so closure into their *absolute* destinations (leading '/' stripped in tar entries).
            - Also resolves any 'extra_sonames' using the root binary's search list, and includes their transitive deps.
            - If 'binary' is not an ELF, the tool adds nothing (the result is just base_tar or an empty tar).""",
)

def _resolve_tags_impl(ctx):
    stable_status = ctx.info_file
    input_lines = ctx.attr.raw_tags
    input_lines_concat = "\n".join(input_lines)
    output_file = ctx.actions.declare_file(ctx.label.name + ".output.txt")

    # Collect files from the package target (if specified)
    package_inputs = []
    if ctx.attr.package:
        package_inputs = ctx.attr.package.files.to_list()

    ctx.actions.run(
        executable = ctx.executable.processor,
        inputs = [stable_status] + package_inputs,
        outputs = [output_file],
        arguments = [
            stable_status.path,
            output_file.path,
            input_lines_concat,
        ],
        mnemonic = "MyCustomRun",
        progress_message = "Resolving OCI tags for {}".format(ctx.label),
    )
    return [
        DefaultInfo(files = depset([output_file])),
    ]

_resolve_tags = rule(
    implementation = _resolve_tags_impl,
    attrs = {
        "raw_tags": attr.string_list(mandatory = True),
        "processor": attr.label(
            executable = True,
            cfg = "exec",
            allow_files = True,
            default = Label("//bazel/pkg:status_substitutor"),
        ),
        "stamp": attr.int(
            default = 1,
            values = [1],
            doc = "1 = always stamp",
        ),
        "package": attr.label(
            mandatory = True,
            allow_files = True,
            doc = "Target whose outputs will trigger this rule when changed",
        ),
    },
)

def _oci_repository_file_impl(ctx):
    env = ctx.attr._oci_publish_env[BuildSettingInfo].value
    dev_repo = ctx.attr._oci_dev_repo[BuildSettingInfo].value
    prod_repo = ctx.attr._oci_prod_repo[BuildSettingInfo].value

    if env == "prod":
        repo_prefix = prod_repo
    elif env == "dev":
        repo_prefix = dev_repo
    else:
        fail("Unsupported oci_publish_env value: %s" % env)

    repo_prefix = repo_prefix.rstrip("/")
    if not repo_prefix:
        fail("Resolved OCI repository prefix is empty.")

    out = ctx.actions.declare_file(ctx.label.name + ".repository.txt")
    ctx.actions.write(
        output = out,
        content = "%s/%s\n" % (repo_prefix, ctx.attr.path),
    )
    return [DefaultInfo(files = depset([out]))]

_oci_repository_file = rule(
    implementation = _oci_repository_file_impl,
    attrs = {
        "path": attr.string(mandatory = True),
        "_oci_publish_env": attr.label(
            default = Label("//bazel/pkg:oci_publish_env"),
        ),
        "_oci_dev_repo": attr.label(
            default = Label("//bazel/pkg:oci_dev_repo"),
        ),
        "_oci_prod_repo": attr.label(
            default = Label("//bazel/pkg:oci_prod_repo"),
        ),
    },
)

def _oci_push_with_dry_run_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".sh")
    script = """#!/usr/bin/env bash
set -euo pipefail

runfiles_dir="${RUNFILES_DIR:-}"
runfiles_manifest="${RUNFILES_MANIFEST_FILE:-}"

if [[ -z "${runfiles_dir}" && -d "$0.runfiles" ]]; then
  runfiles_dir="$0.runfiles"
fi

if [[ -z "${runfiles_manifest}" ]]; then
  if [[ -f "$0.runfiles_manifest" ]]; then
    runfiles_manifest="$0.runfiles_manifest"
  elif [[ -n "${runfiles_dir}" && -f "${runfiles_dir}/MANIFEST" ]]; then
    runfiles_manifest="${runfiles_dir}/MANIFEST"
  fi
fi

function _resolveRunfile() {
  local logical_path="$1"

  if [[ -n "${runfiles_dir}" && -e "${runfiles_dir}/${logical_path}" ]]; then
    printf "%s\\n" "${runfiles_dir}/${logical_path}"
    return 0
  fi

  if [[ -n "${runfiles_manifest}" ]]; then
    local resolved_path
    resolved_path="$(grep -sm1 "^${logical_path} " "${runfiles_manifest}" | cut -d' ' -f2-)"
    if [[ -n "${resolved_path}" ]]; then
      printf "%s\\n" "${resolved_path}"
      return 0
    fi
  fi

  echo "ERROR: unable to resolve runfile ${logical_path}" >&2
  exit 1
}

push_bin="$(_resolveRunfile "_lumesof_oci_push_bin")"
repo_file="$(_resolveRunfile "_lumesof_oci_repo_file")"
tags_file="$(_resolveRunfile "_lumesof_oci_tags_file")"

dry_run=0
forwarded=()
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    dry_run=1
    continue
  fi
  forwarded+=("$arg")
done

if [[ "${dry_run}" -eq 1 ]]; then
  repo="$(tr -d '\\n' < "${repo_file}")"
  echo "[dry-run] repository: ${repo}"
  echo "[dry-run] remote tags:"
  if [[ -s "${tags_file}" ]]; then
    sed 's/^/  - /' "${tags_file}"
  else
    echo "  (none)"
  fi
  exit 0
fi

if [[ -n "${runfiles_dir}" ]]; then
  export RUNFILES_DIR="${runfiles_dir}"
fi
if [[ -n "${runfiles_manifest}" ]]; then
  export RUNFILES_MANIFEST_FILE="${runfiles_manifest}"
fi

exec "${push_bin}" "${forwarded[@]}"
"""
    ctx.actions.write(
        output = out,
        content = script,
        is_executable = True,
    )

    runfiles = ctx.runfiles(
        files = [
            ctx.executable.push,
            ctx.file.repository_file,
            ctx.file.remote_tags,
        ],
        root_symlinks = {
            "_lumesof_oci_push_bin": ctx.executable.push,
            "_lumesof_oci_repo_file": ctx.file.repository_file,
            "_lumesof_oci_tags_file": ctx.file.remote_tags,
        },
    )
    push_di = ctx.attr.push[DefaultInfo]
    if push_di.default_runfiles:
        runfiles = runfiles.merge(push_di.default_runfiles)
    if push_di.data_runfiles:
        runfiles = runfiles.merge(push_di.data_runfiles)

    return [DefaultInfo(
        executable = out,
        files = depset([out]),
        runfiles = runfiles,
    )]

_oci_push_with_dry_run = rule(
    implementation = _oci_push_with_dry_run_impl,
    attrs = {
        "push": attr.label(
            mandatory = True,
            executable = True,
            cfg = "target",
        ),
        "repository_file": attr.label(
            mandatory = True,
            allow_single_file = True,
        ),
        "remote_tags": attr.label(
            mandatory = True,
            allow_single_file = True,
        ),
    },
    executable = True,
)

def _lumesof_repo(path):
    """Legacy helper for LORe default registry path selection."""
    return select({
        "//bazel/pkg:oci_publish_env_dev": "us-central1-docker.pkg.dev/lumesof-infra-dev/dev-docker-registry" + "/" + path,
        "//bazel/pkg:oci_publish_env_prod": "us-central1-docker.pkg.dev/lumesof-infra-build/build-docker-registry" + "/" + path,
        "//conditions:default": "us-central1-docker.pkg.dev/lumesof-infra-dev/dev-docker-registry" + "/" + path,
    })

# Internal consumers need this helper, but Starlark disallows importing symbols
# prefixed with "_" across files.
lumesof_repo = _lumesof_repo

def _lumesof_tags():
    """Generates standard tags selected by --//bazel/pkg:oci_publish_env=dev|prod."""
    tag = "{STABLE_BUILD_SCM_REVISION}"
    tags = select({
        "//bazel/pkg:oci_publish_env_dev": [tag, "{STABLE_BUILD_USER}-latest"],
        "//bazel/pkg:oci_publish_env_prod": [tag, "ci-latest"],
        "//conditions:default": [tag, "{STABLE_BUILD_USER}-latest"],
    })
    return tags


lumesof_common_extra_sonames = [
    "libnss_dns.so.2",  # DNS NSS backend
    "libnss_files.so.2",  # /etc/{passwd,group,hosts} backend
    "libresolv.so.2",  # getaddrinfo(), resolver routines
]

def _oci_image_wrapper_impl(ctx):
    """Implementation for _oci_image_wrapper.

    Forwards DefaultInfo from an oci_image and exposes its digest file
    under OutputGroupInfo['digest'].

    Args:
      ctx: Rule context.

    Returns:
      DefaultInfo and OutputGroupInfo.
    """
    img_def = ctx.attr.image[DefaultInfo]
    digest_file = ctx.file.digest
    return [
        DefaultInfo(
            files = img_def.files,
            runfiles = img_def.default_runfiles,
        ),
        OutputGroupInfo(
            digest = depset([digest_file]),
        ),
    ]

_oci_image_wrapper = rule(
    implementation = _oci_image_wrapper_impl,
    attrs = {
        "image": attr.label(mandatory = True),
        "digest": attr.label(allow_single_file = True, mandatory = True),
    },
)

def lumesof_oci_image(
        name,
        binary,
        package_dir = "/app",
        base = "@lumesof_base_image",
        extra_sonames = [],
        keep_empty_dirs = False,
        labels = None):
    """Builds a runnable OCI image for a single Bazel binary and wraps it.

    This macro reproduces the existing lumesof image flow and then wraps the
    inner oci_image with _oci_image_wrapper so callers can access the digest
    via OutputGroupInfo["digest"] without referencing a sibling target.

    Args:
      name: Name of the final exported image target.
      binary: Label of the executable target to package (for example, a
        py_binary, cc_binary, sh_binary, or go_binary). Must expose DefaultInfo
        with an executable and runfiles.
      package_dir: Absolute path inside the container where the app is laid
        out. The macro places the launcher at <package_dir>/run and the binary
        at <package_dir>/.bin/<exe>.
      base: Base image for the container, typically created by oci.pull
        (for example, "@lumesof_base_image").
      extra_sonames: Additional shared object names to include in the ELF
        closure when the binary is an ELF.
      keep_empty_dirs: Whether to preserve empty directories found inside tree
        artifacts when building the layer tar via binary_tar.
      labels: Optional OCI config labels to set during image construction.
        This is forwarded directly to rules_oci's oci_image and may be a
        dict or a label to a key=value file.

    Outputs:
      A target named "name" that forwards the oci_image DefaultInfo and exposes
      the digest file under OutputGroupInfo["digest"].
    """
    layer_name = "%s_layer" % name

    binary_tar(
        name = layer_name,
        binary = binary,
        pkg_dir = package_dir,
        launch_script_name = "run",
        keep_empty_dirs = keep_empty_dirs,
    )

    _add_elf_closure(
        name = layer_name + "_closure",
        binary = binary,
        pkg_dir = package_dir,
        base_tar = ":" + layer_name,
        extra_sonames = extra_sonames,
    )

    container_entrypoint_path = "/%s/run" % (package_dir)

    inner_img = name + "__inner_oci_image"
    oci_image(
        name = inner_img,
        base = base,
        tars = [":" + layer_name + "_closure"],
        entrypoint = [
            "/usr/bin/tini",
            "--",
            container_entrypoint_path,
        ],
        labels = labels,
        user = "appuser",
        workdir = "/home/appuser",
    )

    _oci_image_wrapper(
        name = name,
        image = ":" + inner_img,
        digest = ":" + inner_img + ".digest",
    )

def _oci_image_executor_impl(ctx):
    """Implementation for _oci_image_executor.

    Runs a Python tool to generate a standalone executable that embeds the
    manifest digest and runs "docker run localhost/bazel_load:{digest}".

    Args:
      ctx: Rule context.

    Returns:
      DefaultInfo with an executable. No runfiles are required.
    """
    out = ctx.actions.declare_file(ctx.label.name)
    image_name = ctx.attr.image_name

    # Obtain the digest file from the image's output group.
    grp = ctx.attr.image[OutputGroupInfo]
    ds = getattr(grp, "digest", depset())
    files = ds.to_list()
    if not files:
        fail("No digest file available on the image target (output group 'digest').")
    digest_file = files[0]

    tool = ctx.executable.tool

    ctx.actions.run(
        inputs = [digest_file],
        tools = [tool],
        outputs = [out],
        executable = tool,
        arguments = [
            "--image_name",
            image_name,
            "--digest_file",
            digest_file.path,
            "--out",
            out.path,
        ],
        progress_message = "Generating OCI runner script for {}".format(ctx.label),
        mnemonic = "GenOciRunner",
    )

    return DefaultInfo(
        executable = out,
        files = depset([out]),
        runfiles = ctx.runfiles(),  # script is standalone
    )

_oci_image_executor = rule(
    implementation = _oci_image_executor_impl,
    attrs = {
        "image": attr.label(mandatory = True),
        "image_name": attr.string(mandatory = True),
        "tool": attr.label(
            executable = True,
            cfg = "exec",
            default = "//bazel/pkg:oci_executor_tool",
            doc = "Python tool that generates the runner script.",
        ),
    },
    executable = True,
)

def _local_digest_tag_impl(ctx):
    """Implementation for oci_digest_tag.

    Reads the image digest from OutputGroupInfo['digest'] and writes a tag file
    containing one line: "{image_name}:{digest_without_prefix}".
    """
    grp = ctx.attr.image[OutputGroupInfo]
    ds = getattr(grp, "digest", depset())
    files = ds.to_list()
    if not files:
        fail("No digest file available on the image target (output group 'digest').")
    digest_file = files[0]

    out = ctx.actions.declare_file(ctx.label.name + ".tags.txt")

    ctx.actions.run_shell(
        inputs = [digest_file],
        outputs = [out],
        command = """
set -euo pipefail
d="$(cat "{digest_path}")"
# Strip optional "sha256:" prefix so the tag remains syntactically valid.
d="${{d#sha256:}}"
printf "%s:%s\\n" "{image_name}" "$d" > "{out_path}"
""".format(
            digest_path = digest_file.path,
            image_name = ctx.attr.image_name,
            out_path = out.path,
        ),
        mnemonic = "MakeDigestTag",
        progress_message = "Making digest tag for {}".format(ctx.label),
    )

    return DefaultInfo(files = depset([out]))

_local_digest_tag = rule(
    implementation = _local_digest_tag_impl,
    attrs = {
        "image": attr.label(
            mandatory = True,
            doc = "OCI image wrapper target that exposes OutputGroupInfo['digest'].",
        ),
        "image_name": attr.string(
            mandatory = True,
            doc = "Repository name used as the tag prefix (e.g., 'localhost/app').",
        ),
    },
    doc = """Produces a tag file with a single line: '{image_name}:{digest}'.

The digest is read from the upstream target’s OutputGroupInfo['digest'] and any
leading 'sha256:' is removed before forming the tag.""",
)

def lumesof_oci_local_executor(name, image):
    """Creates a multirun target that loads an OCI image, then runs it by tag.

    This macro:
      1) Creates a single-line repo_tags file using the image's digest (tag = digest sans 'sha256:').
      2) Creates an oci_load target to load the image into the local daemon.
      3) Creates an executable that runs the image using that tag.
      4) Exposes a `multirun` target named `name` that runs (2) then (3).

    Args:
      name: Name of the exported executable target to create.
      image: Wrapped OCI image produced by lumesof_oci_image; must expose
        OutputGroupInfo["digest"].
    """

    # 1) repo_tags (exactly one line) – e.g. "localhost/bazel_load:<64-hex>"
    tags = name + "__repo_tags"
    _local_digest_tag(
        name = tags,
        image = image,
        image_name = "localhost/bazel_load",
        visibility = ["//visibility:private"],
    )

    # 2) Loader (runnable)
    loader = name + ".load"
    oci_load(
        name = loader,
        image = image,
        repo_tags = ":" + tags,
        visibility = ["//visibility:private"],
    )

    # 3) Runner (runnable) – uses same repo name, tag = digest sans "sha256:"
    runner = name + "__exec"
    _oci_image_executor(
        name = runner,
        image = image,
        image_name = "localhost/bazel_load",
        visibility = ["//visibility:private"],
    )

    # Wrap both runnables as multirun commands.
    cmd_load = name + "__cmd_load"
    command(
        name = cmd_load,
        command = ":" + loader,
        visibility = ["//visibility:private"],
    )

    cmd_run = name + "__cmd_run"
    command(
        name = cmd_run,
        command = ":" + runner,
        visibility = ["//visibility:private"],
    )

    # 4) One `bazel run :name` executes load → run (sequential).
    multirun(
        name = name,
        commands = [
            ":" + cmd_load,
            ":" + cmd_run,
        ],
    )

def lumesof_oci_image_publish(name, image, **kwargs):
    """Publishes an OCI image to Artifact Registry with standard Lumesof tags.

    Args:
      name: Name of the oci_push target to create.
      image: Label of the OCI image to push (typically created by lumesof_oci_image).
      **kwargs: Additional attributes forwarded to oci_push.

    Outputs:
      An oci_push target named "name".
    """
    prefix = native.package_name().replace("/", "-")
    lstr = str(image)
    internal_rule_name = "_" + name + "_repo_tags_"
    internal_repo_file_name = "_" + name + "_repository_"
    internal_push_name = "_" + name + "_push_"
    visibility = kwargs.pop("visibility", None)

    _resolve_tags(
        name = internal_rule_name,
        raw_tags = _lumesof_tags(),
        package = image,
    )

    _oci_repository_file(
        name = internal_repo_file_name,
        path = prefix + "-" + lstr.split(":")[-1],
    )

    oci_push(
        name = internal_push_name,
        image = image,
        repository_file = ":" + internal_repo_file_name,
        remote_tags = ":" + internal_rule_name,
        visibility = ["//visibility:private"],
        tags = ["manual"],
        **kwargs
    )

    wrapper_kwargs = {}
    if visibility != None:
        wrapper_kwargs["visibility"] = visibility

    _oci_push_with_dry_run(
        name = name,
        push = ":" + internal_push_name,
        repository_file = ":" + internal_repo_file_name,
        remote_tags = ":" + internal_rule_name,
        tags = ["manual"],
        **wrapper_kwargs
    )


def _default_app_name(ctx):
    """If app_name unset, fall back to target name."""
    return ctx.attr.app_name if ctx.attr.app_name else ctx.label.name

def _app_image_impl(ctx):
    tar_in = ctx.file.tar
    out_name = ctx.label.name + ".appimage"
    img_out = ctx.actions.declare_file(out_name)

    # Resolve absolute path to appimagetool from the registered system toolchain.
    sys = ctx.toolchains["//bazel/sys:system_toolchain_type"].systeminfo
    appimage_tool_path = sys.appimagetool  # absolute path in your CAS mount

    builder = ctx.executable._builder

    # Optional files
    icon = ctx.file.icon
    desktop = ctx.file.desktop
    desktop_template = ctx.file.desktop_template

    # Normalize string args
    sde = ctx.attr.source_date_epoch if ctx.attr.source_date_epoch else "0"
    version = ctx.attr.version if ctx.attr.version else "0"

    args = [
        "--input-tar",
        tar_in.path,
        "--output",
        img_out.path,
        "--app-name",
        _default_app_name(ctx),
        "--arch",
        ctx.attr.arch,
        "--package-dir",
        ctx.attr.package_dir,
        "--compression",
        ctx.attr.compression,
        "--source-date-epoch",
        sde,
        "--version",
        version,
        "--appimage-tool-path",
        appimage_tool_path,
    ]

    inputs = [tar_in]
    if icon:
        args.extend(["--icon", icon.path])
        inputs.append(icon)
    if desktop:
        args.extend(["--desktop", desktop.path])
        inputs.append(desktop)
    if desktop_template:
        args.extend(["--desktop-template", desktop_template.path])
        inputs.append(desktop_template)

    # Do NOT set SOURCE_DATE_EPOCH in env here; appimagetool's internal mksquashfs
    # passes -mkfs-time, and combining both conflicts. We already normalized file mtimes.
    ctx.actions.run(
        executable = builder,
        arguments = args,
        inputs = inputs,
        tools = [builder],
        outputs = [img_out],
        mnemonic = "LumesofAppImageBuild",
        progress_message = "Building deterministic app image: %s" % out_name,
    )

    return [DefaultInfo(
        files = depset([img_out]),
        executable = img_out,  # make the rule runnable
        runfiles = ctx.runfiles(),  # no special runfiles needed
    )]

_app_image = rule(
    implementation = _app_image_impl,
    attrs = {
        "tar": attr.label(
            mandatory = True,
            allow_single_file = [".tar"],
        ),
        "app_name": attr.string(),
        "version": attr.string(),
        "source_date_epoch": attr.string(),
        "package_dir": attr.string(default = "/app"),
        "arch": attr.string(default = "x86_64", values = ["x86_64", "aarch64"]),
        "compression": attr.string(default = "zstd", values = ["zstd", "xz"]),
        "icon": attr.label(allow_single_file = [".png", ".svg", ".ico"]),
        "desktop": attr.label(allow_single_file = [".desktop"]),
        "desktop_template": attr.label(allow_single_file = [".tmpl", ".template"]),
        "_builder": attr.label(
            default = Label("//bazel/pkg:app_image_tool"),
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = ["//bazel/sys:system_toolchain_type"],
    executable = True,  # allow `bazel run :<app_image>`
    doc = "Turn a `binary_tar` output into an AppImage using appimagetool (from the system toolchain).",
)

def lumesof_app_image(
        name,
        binary,
        package_dir = "/app",
        keep_empty_dirs = False,
        app_name = None,
        version = "{STABLE_BUILD_SCM_REVISION}",
        arch = "x86_64",
        compression = "zstd",
        icon = None,
        desktop = None,
        desktop_template = None,
        source_date_epoch = None):
    """
    Build a deterministic AppImage from a Bazel binary using `binary_tar` + `_app_image`.

    This macro first creates a reproducible tarball via `binary_tar`, then expands it
    into a deterministic AppImage-like artifact with `_app_image`. The launcher and
    layout mirror the OCI path: `<package_dir>/run` execs the packaged binary under
    `<package_dir>/.bin/<exe>`.

    Args:
        name: Name of the resulting app image target (emits `<name>.appimage`).
        binary: Executable label to package (py_binary/cc_binary/sh_binary/go_binary, etc.).
        package_dir: Install root inside the image (default: "/app").
        keep_empty_dirs: Preserve empty dirs from tree artifacts (forwarded to `binary_tar`).
        app_name: Friendly app name (defaults to `name`).
        version: Version string embedded in metadata (default stamps commit SHA).
        arch: "x86_64" or "aarch64".
        compression: "zstd" (fast/repro) or "xz" (smaller/slower).
        icon: Label of an icon file (.png/.svg/.ico).
        desktop: Label of a ready `.desktop` file (if you already have one).
        desktop_template: Template consumed by the builder to synthesize `.desktop`.
        source_date_epoch: Unix epoch seconds for mtimes; defaults to "0" if unset.

    Outputs:
        A target `name` producing `<name>.appimage` with:
          • Launcher at `<package_dir>/run`
          • Fully deterministic filesystem & metadata
    """
    layer_name = "%s_layer" % name

    binary_tar(
        name = layer_name,
        binary = binary,
        pkg_dir = package_dir,
        launch_script_name = "run",
        keep_empty_dirs = keep_empty_dirs,
    )

    _app_image(
        name = name,
        tar = ":" + layer_name,
        app_name = app_name if app_name else name,
        version = version,
        source_date_epoch = source_date_epoch if source_date_epoch else "0",
        package_dir = package_dir,
        arch = arch,
        compression = compression,
        icon = icon,
        desktop = desktop,
        desktop_template = desktop_template,
    )
