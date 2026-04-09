"""
This file defines Lumesof custom rules and macros for Python.
"""
load("@rules_python//python:defs.bzl", "PyInfo", "py_binary", "py_library", "py_test")
load("@rules_python//python:packaging.bzl", "py_wheel")

# Define the LumesofPipRequirementsInfo provider
# This provider will carry the collection of validated requirements files
LumesofPipRequirementsInfo = provider(
    fields = ["requirements"], # 'requirements' will be a depset of File objects
    doc = "Provider for validated pip requirements files.",
)

def _lumesof_pip_req_impl(ctx):
    all_requirements_files = depset(order = "default") # Use a depset to gather all files transitively
    files_for_direct_validation = []
    files_for_dep_propagation = []

    # Iterate through each dependency specified in the 'requirements' attribute
    for dep_target in ctx.attr.requirements:
        # Check if the dependency provides our custom LumesofPipRequirementsInfo provider
        if LumesofPipRequirementsInfo in dep_target:
            # If it does, add its requirements (which are already a depset)
            # to our collection using depset's transitive argument
            all_requirements_files = depset(
                transitive = [all_requirements_files, dep_target[LumesofPipRequirementsInfo].requirements],
                order = "default",
            )
            files_for_dep_propagation += dep_target[DefaultInfo].files.to_list()
        # Check if the dependency provides the standard DefaultInfo provider
        elif DefaultInfo in dep_target:
            # If it does, get its default files
            current_dep_files = dep_target[DefaultInfo].files.to_list()
            # Add these files to the overall collection
            all_requirements_files = depset(current_dep_files, transitive = [all_requirements_files], order = "default")
            # Also add these files to the list that will be directly validated by this rule's action
            files_for_direct_validation += current_dep_files
        else:
            # If the dependency provides neither, it's an invalid input for this rule
            fail("Invalid dependency: '{}' must provide DefaultInfo or LumesofPipRequirementsInfo. Got: {}".format(
                dep_target.label,
                ", ".join([str(p) for p in dep_target.providers]), # List the providers it actually has
            ))

    # Define the output marker file. This is crucial to ensure the validation action runs.
    transitive_validation_marker = ctx.actions.declare_file(ctx.label.name + "_validation_transitive")
    validation_marker = ctx.actions.declare_file(ctx.label.name + "_validation_completed.txt")

    # Make sure that validation markers exist
    ctx.actions.run_shell(
        inputs = files_for_dep_propagation,
        outputs = [transitive_validation_marker],
        command = """
touch {transitive_marker}
""".format(transitive_marker = transitive_validation_marker.path),
        mnemonic = "LumesofTransitiveValidation",
        progress_message = "Validating pip requirements files for {}".format(ctx.label.name),
    )

    # Only create the validation action if there are files to validate
    if files_for_direct_validation:
        # Prepare the list of input file paths for the shell script.
        # Using a here-doc approach for robustness against spaces in filenames.
        input_file_paths_str = "\n".join([f.path for f in files_for_direct_validation])

        validation_script_content = """#!/bin/bash
set -euo pipefail

echo "Running the shell script"
# Read each file path from the here-document
while IFS= read -r f; do
    lineno=0
    # Process each line of the requirement file
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        # Remove leading/trailing whitespace
        trimmed_line=$(echo "$line" | sed -E 's/^[[:space:]]+|[[[:space:]]]$//g')
        
        # Check for '-r' at the beginning of a non-empty, trimmed line
        # Use a more specific regex to ensure it's at the beginning of the line
        if [[ -n "$trimmed_line" && "$trimmed_line" =~ ^-r ]]; then
            echo "ERROR: Invalid line in '$f':$lineno: Recursive requirements ('-r') are not allowed." >&2
            exit 1
        fi
    done < "$f"
done <<'EOF_INPUT_FILES'
{input_file_list}
EOF_INPUT_FILES

# If validation passes, create the marker file
touch {output_marker}
""".format(
            input_file_list = input_file_paths_str,
            output_marker = validation_marker.path,
        )

        # Create the validation script file (optional, but good for debugging complex scripts)
        validation_script = ctx.actions.declare_file(ctx.label.name + "_validation_script.sh")
        ctx.actions.write(
            output = validation_script,
            content = validation_script_content,
            is_executable = True,
        )

        # Execute the validation script
        ctx.actions.run_shell(
            inputs = files_for_direct_validation + [validation_script, transitive_validation_marker],
            outputs = [validation_marker],
            command = validation_script.path, # Run the generated script
            mnemonic = "LumesofValidateRequirements",
            progress_message = "Validating pip requirements files for {}".format(ctx.label.name),
        )
    else:
        # If no files were provided for validation, still create the marker file
        # to ensure the build completes successfully and the rule's target exists.
        ctx.actions.run_shell(
            inputs = [transitive_validation_marker],
            outputs = [validation_marker],
            command = """
touch {output_marker}
""".format(output_marker = validation_marker.path),
            mnemonic = "LumesofValidation",
            progress_message = "Validating pip requirements files for {}".format(ctx.label.name),
        )

    # Return the DefaultInfo provider with the validation marker file
    # and the LumesofPipRequirementsInfo provider with the collected files.
    return [
        DefaultInfo(files = depset([validation_marker])),
        LumesofPipRequirementsInfo(requirements = all_requirements_files),
    ]

def _req_lock_impl(ctx):
    local_pkg_file = ctx.file.local_pkg
    lockfile = ctx.actions.declare_file(ctx.label.name + ".lock")
    
    # Flatten all requirement files from LumesofPipRequirementsInfo
    requirement_files = []
    files_for_dep_propagation = []
    for dep in ctx.attr.pip_reqs:
        reqs = dep[LumesofPipRequirementsInfo].requirements
        requirement_files += reqs.to_list()
        files_for_dep_propagation += dep[DefaultInfo].files.to_list()

    # Write manifest as JSON
    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest.json")
    json_lines = [
        '{{"short_path": "{}", "path": "{}"}}'.format(f.short_path, f.path)
        for f in requirement_files
    ]
    json_content = "[\n" + ",\n".join(json_lines) + "\n]"
    
    ctx.actions.run_shell(
        inputs = files_for_dep_propagation,
        outputs = [manifest],
        command = """
echo '{content}' > {out}
""".format(content = json_content, out = manifest.path),
        mnemonic = "LumesofTransitiveValidation",
        progress_message = "Validating pip requirements files for {}".format(ctx.label.name),
    )

    # Collect local wheel files
    wheel_files = [f for w in ctx.attr.wheels for f in w.files.to_list()]

    # Run lockfile generator
    runner_args = [local_pkg_file.path, lockfile.path, manifest.path]
    for wheel_file in wheel_files:
        runner_args.extend(["--wheel", wheel_file.path])
    for index_url in ctx.attr.additional_indexes:
        runner_args.extend(["--additional-index", index_url])

    ctx.actions.run(
        inputs = [local_pkg_file, manifest] + requirement_files + wheel_files,
        outputs = [lockfile],
        executable = ctx.executable.pkg_runner,
        arguments = runner_args,
        mnemonic = "GenerateLockfile",
        progress_message = "Generating requirements lockfile",
    )

    return DefaultInfo(files = depset([lockfile]))

def _pip_install_impl(ctx):
    install_dir = ctx.actions.declare_directory(ctx.label.name + "_site")

    # Collect local wheel files
    wheel_files = [f for w in ctx.attr.wheels for f in w.files.to_list()]

    runner_args = [
        ctx.file.local_pkg.path,
        ctx.file.lockfile.path,
        install_dir.path,
    ]
    for wheel_file in wheel_files:
        runner_args.extend(["--wheel", wheel_file.path])
    for index_url in ctx.attr.additional_indexes:
        runner_args.extend(["--additional-index", index_url])

    ctx.actions.run(
        inputs = [ctx.file.local_pkg, ctx.file.lockfile] + wheel_files,
        outputs = [install_dir],
        executable = ctx.executable.pkg_runner,
        arguments = runner_args,
        mnemonic = "InstallPipPackages",
        progress_message = "Installing Python packages into site directory",
    )

    # Stable logical runfiles path derived from this target's label.
    pkg = ctx.label.package  # "" at repo root
    logical = (pkg + "/" if pkg else "") + ctx.label.name + "_site"

    # Put the tree artifact at that logical path in runfiles.
    rf = ctx.runfiles(
        transitive_files = depset([install_dir]),
        root_symlinks = {logical: install_dir},
    )

    return [
        DefaultInfo(
            files = depset([install_dir]),
            runfiles = rf,
        ),
        PyInfo(
            transitive_sources = depset(),       # no .py sources of our own
            imports = depset([logical]),         # <-- adds to PYTHONPATH
            uses_shared_libraries = False,
            has_py2_only_sources = False,
            has_py3_only_sources = True,
        ),
    ]

def _lumesof_py_library_impl(ctx):
    dep = ctx.attr.dep
    pip_req = ctx.attr.pip_req
    

    if PyInfo not in dep:
        fail("Dependency %s does not provide the required provider PyInfo" % dep.label)
    
    if DefaultInfo not in dep:
        fail("Dependency %s does not provide the required provider DefaultInfo" % dep.label)

    if LumesofPipRequirementsInfo not in pip_req:
        fail("Dependency %s does not provide the required provider LumesofPipRequirementsInfo" % dep.label)

    dep_default_info = dep[DefaultInfo]
    pip_req_default_info = pip_req[DefaultInfo]
    
    return [
        dep[PyInfo],
        DefaultInfo(
            files = depset(
                direct = [],
                transitive = [dep_default_info.files, pip_req_default_info.files]),
            # Preserve the original runfiles object. Reconstructing from file lists
            # drops root_symlinks/symlink metadata required by some Python targets.
            default_runfiles = dep_default_info.default_runfiles,
            data_runfiles = dep_default_info.data_runfiles,
        ),
        pip_req[LumesofPipRequirementsInfo],
    ]

def _wheel_requirements_impl(ctx):
    dep = ctx.attr.dep
    requirement_files = dep[LumesofPipRequirementsInfo].requirements.to_list()
    dep_default_files = dep[DefaultInfo].files.to_list()

    requirement_paths = sorted([f.path for f in requirement_files])
    requirement_path_list = ctx.actions.declare_file(ctx.label.name + ".requirements.paths.txt")
    ctx.actions.write(
        output = requirement_path_list,
        content = "\n".join(requirement_paths) + ("\n" if requirement_paths else ""),
    )

    flattened_requirements = ctx.actions.declare_file(ctx.label.name + ".flattened.requirements.txt")
    ctx.actions.run_shell(
        inputs = requirement_files + dep_default_files + [requirement_path_list],
        outputs = [flattened_requirements],
        command = """
set -euo pipefail

requirements_list="$1"
output_file="$2"
declare -A seen=()
: > "$output_file"

while IFS= read -r req_file || [ -n "$req_file" ]; do
  if [[ -z "$req_file" ]]; then
    continue
  fi

  lineno=0
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    trimmed_line=$(echo "$line" | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')

    if [[ -z "$trimmed_line" || "$trimmed_line" == \\#* ]]; then
      continue
    fi

    if [[ "$trimmed_line" =~ ^(-r|--requirement)([[:space:]]|$) ]]; then
      echo "ERROR: Invalid line in '$req_file':$lineno: Recursive requirements are not allowed for wheel metadata." >&2
      exit 1
    fi

    if [[ -z "${seen[$trimmed_line]+x}" ]]; then
      seen[$trimmed_line]=1
      echo "$trimmed_line" >> "$output_file"
    fi
  done < "$req_file"
done < "$requirements_list"
""",
        arguments = [requirement_path_list.path, flattened_requirements.path],
        mnemonic = "LumesofFlattenWheelRequirements",
        progress_message = "Flattening wheel requirements for {}".format(ctx.label.name),
    )

    validation_marker = ctx.actions.declare_file(ctx.label.name + ".validation.ok")
    validation_lock = ctx.actions.declare_file(ctx.label.name + ".validation.lock")
    if ctx.attr.validate_resolve:
        validation_manifest = ctx.actions.declare_file(ctx.label.name + ".validation.manifest.json")
        ctx.actions.write(
            output = validation_manifest,
            content = '[{{"short_path":"requirements.txt","path":"{}"}}]\n'.format(flattened_requirements.path),
        )

        ctx.actions.run(
            inputs = [ctx.file.local_pkg, validation_manifest, flattened_requirements],
            outputs = [validation_lock],
            executable = ctx.executable.pkg_runner,
            arguments = [ctx.file.local_pkg.path, validation_lock.path, validation_manifest.path],
            mnemonic = "LumesofValidateWheelRequirements",
            progress_message = "Resolving wheel requirements for {}".format(ctx.label.name),
        )
    else:
        ctx.actions.run_shell(
            inputs = [flattened_requirements],
            outputs = [validation_lock],
            command = "touch {}".format(validation_lock.path),
            mnemonic = "LumesofSkipWheelRequirementsResolve",
            progress_message = "Skipping wheel requirement resolve validation for {}".format(ctx.label.name),
        )

    ctx.actions.run_shell(
        inputs = [validation_lock],
        outputs = [validation_marker],
        command = "touch {}".format(validation_marker.path),
        mnemonic = "LumesofWheelRequirementsValidationMarker",
        progress_message = "Marking wheel requirement validation for {}".format(ctx.label.name),
    )

    wheel_requirements = ctx.actions.declare_file(ctx.label.name + ".requirements.txt")
    ctx.actions.run_shell(
        inputs = [flattened_requirements, validation_marker],
        outputs = [wheel_requirements],
        command = "cp {src} {dst}".format(src = flattened_requirements.path, dst = wheel_requirements.path),
        mnemonic = "LumesofEmitWheelRequirements",
        progress_message = "Emitting wheel requirements for {}".format(ctx.label.name),
    )

    return DefaultInfo(files = depset([wheel_requirements]))

def _normalize_wheel_path(short_path):
    # short_path may be relative ("../${repository_root}/foo.py"), which is not
    # a valid wheel path. Strip the leading repository segment.
    path = short_path
    if path.startswith("..") and len(path) >= 3:
        separator = path[2]
        path = path[3:]
        pos = path.find(separator)
        path = path[pos + 1:]
    return path

def _strip_import_prefix(path, prefix):
    if not prefix:
        return None
    if path == prefix:
        return ""
    canonical_prefix = prefix + "/"
    if path.startswith(canonical_prefix):
        return path[len(canonical_prefix):]
    return None

def _canonicalize_wheel_source_path(short_path, import_roots):
    path = _normalize_wheel_path(short_path)
    roots = sorted(import_roots, key = lambda x: len(x), reverse = True)
    for root in roots:
        stripped = _strip_import_prefix(path, root)
        if stripped != None and stripped != "":
            path = stripped
            break

    for marker in ["/_py_pb2_pb/", "/_py_grpc_pb/"]:
        idx = path.rfind(marker)
        if idx != -1:
            path = path[idx + len(marker):]
            break

    package_roots = ["lumesof", "lumecode", "google", "grpc"]
    best_idx = -1
    best_root = None
    for root in package_roots:
        marker = "/" + root + "/"
        idx = path.rfind(marker)
        if idx > best_idx:
            best_idx = idx
            best_root = root
    if best_idx != -1:
        return path[best_idx + 1:]
    if best_root and path.startswith(best_root + "/"):
        return path

    site_packages_marker = "/site-packages/"
    site_packages_idx = path.rfind(site_packages_marker)
    if site_packages_idx != -1:
        return path[site_packages_idx + len(site_packages_marker):]
    if path.startswith("site-packages/"):
        return path[len("site-packages/"):]
    return path

def _lumesof_py_package_impl(ctx):
    dep = ctx.attr.dep
    py_info = dep[PyInfo]
    import_roots = [r.rstrip("/") for r in py_info.imports.to_list()]
    include_roots = dict([(r, True) for r in ctx.attr.include_roots])

    source_files = py_info.transitive_sources.to_list() + py_info.transitive_pyi_files.to_list()
    if not source_files:
        return DefaultInfo(files = depset())

    path_to_source = {}
    for src in sorted(source_files, key = lambda f: f.short_path):
        canonical = _canonicalize_wheel_source_path(src.short_path, import_roots)
        if canonical.startswith("../"):
            fail("Invalid canonical wheel path '{}' for source '{}'".format(canonical, src.short_path))
        first_segment = canonical.split("/", 1)[0]
        if include_roots and first_segment not in include_roots:
            continue
        # Duplicates can appear when multiple generated targets emit the same
        # canonical import path. Keep the first one deterministically.
        if canonical in path_to_source:
            continue
        path_to_source[canonical] = src

    outputs = []
    for canonical in sorted(path_to_source.keys()):
        src = path_to_source[canonical]
        out = ctx.actions.declare_file("{name}.pkg/{path}".format(name = ctx.label.name, path = canonical))
        ctx.actions.symlink(
            output = out,
            target_file = src,
        )
        outputs.append(out)

    return DefaultInfo(files = depset(outputs))

# Define the custom rule
_lumesof_pip_req = rule(
    implementation = _lumesof_pip_req_impl,
    attrs = {
        "requirements": attr.label_list(
            doc = "Labels of targets providing DefaultInfo or LumesofPipRequirementsInfo, or direct file targets (.txt files).",
            allow_files = [".txt"], # Allow direct .txt files for requirements
            providers = [[DefaultInfo], [LumesofPipRequirementsInfo]], # Explicitly declare which providers are accepted
        ),
    },
    provides = [LumesofPipRequirementsInfo], # Explicitly declare that this rule provides LumesofPipRequirementsInfo
)


_lumesof_req_lock = rule(
    implementation = _req_lock_impl,
    attrs = {
        "pip_reqs": attr.label_list(
            providers = [ [LumesofPipRequirementsInfo] ],
        ),
        "wheels": attr.label_list(
            allow_files = [".whl"],
        ),
        "additional_indexes": attr.string_list(
            doc = "Additional Python package indexes consulted after local wheels and before PyPI.",
        ),
        "local_pkg": attr.label(
            allow_single_file = True,
            default = "//bazel/python:artifacts/pytool_tar.tgz",
        ),
        "pkg_runner": attr.label(
            executable = True,
            cfg = "exec",
            allow_files = True,
            default = "//bazel/python:lock_runner",
        ),
    },
)

lumesof_pip_install = rule(
    implementation = _pip_install_impl,
    attrs = {
        "lockfile": attr.label(allow_single_file = True),
        "wheels": attr.label_list(
            allow_files = [".whl"],
        ),
        "additional_indexes": attr.string_list(
            doc = "Additional Python package indexes consulted after local wheels and before PyPI.",
        ),
        "local_pkg": attr.label(
            allow_single_file = True,
            default = "//bazel/python:artifacts/pytool_tar.tgz",
        ),
        "pkg_runner": attr.label(
            executable = True,
            cfg = "exec",
            allow_files = True,
            default = "//bazel/python:install_runner",
        ),
    },
)

_lumesof_py_library = rule(
    implementation = _lumesof_py_library_impl,
    attrs = {
        "dep": attr.label(
        ),
        "pip_req": attr.label(
        ),
    },
)

_lumesof_wheel_requirements = rule(
    implementation = _wheel_requirements_impl,
    attrs = {
        "dep": attr.label(
            providers = [[LumesofPipRequirementsInfo]],
        ),
        "validate_resolve": attr.bool(
            default = False,
        ),
        "local_pkg": attr.label(
            allow_single_file = True,
            default = "//bazel/python:artifacts/pytool_tar.tgz",
        ),
        "pkg_runner": attr.label(
            executable = True,
            cfg = "exec",
            allow_files = True,
            default = "//bazel/python:lock_runner",
        ),
    },
)

_lumesof_py_package = rule(
    implementation = _lumesof_py_package_impl,
    attrs = {
        "dep": attr.label(
            providers = [[PyInfo]],
        ),
        "include_roots": attr.string_list(
            default = [],
        ),
    },
)

def lumesof_req_lock(
        name,
        requirements = None,
        wheels = None,
        additional_indexes = None,
        visibility = None,
        tags = None):
  new_label = "_lumesof_internal_" + name + "_pip_req"
  _lumesof_pip_req(
      name = new_label,
      requirements = requirements,
      visibility = ["//visibility:private"]
  )

  _lumesof_req_lock(
    name = name,
    pip_reqs = [":" + new_label],
    wheels = wheels or [],
    additional_indexes = additional_indexes or [],
    visibility = visibility,
    tags = tags,
  )

def lumesof_py_binary(
        name,
        srcs,
        lumesof_deps = [],
        std_deps = None,
        data = [],
        deps = None,
        main = None,
        pip_install = None,
        visibility = None,
        legacy_create_init = False,
        **kwargs):
    """
    A macro that creates a Python binary with integrated pip dependencies.

    Args:
        name: The name of the resulting py_binary target. This will be the
              name by which you refer to your final executable (e.g., //app:my_app).
        srcs: List of source files for the Python application's logic. These are
              combined with the launcher stub's source.
        lumesof_deps: List of Bazel dependencies that provide
              LumesofRequirements provider
        std_deps: Standard python dependencies (like py_library, grpc_py_library, etc.)
        data: List of additional data files for the Python application, to be
              included in its runfiles.
        main: The LABEL of the Python script (e.g., "//app:my_app.py") that serves as the
              application's entry point. This label will be used to calculate the
              Python module name for execution by the launcher stub. (Mandatory)
        pip_install: Optional label of a rule (e.g., your `my_pip_install_rule`) that
                  provides pip-installed packages as a single directory within its
                  `DefaultInfo.run_files` depset. If provided, the stub will add
                  these packages to `sys.path`.
        visibility: The visibility of the generated py_binary target. This
                    controls which other targets can depend on this binary.
                    (e.g., `["//visibility:public"]`, `["//foo:__subpackages__"]`).
        deps: Must be None to prevent accidental inclusion of legacy dependencies.
        legacy_create_init: Must be false.
        **kwargs: Additional keyword arguments to pass directly to the underlying
                  standard `py_binary` rule (e.g., `args`, `python_version`,
                  `testonly`, `tags`, etc.).
    """
    if deps != None:
        fail("Invalid attribute \"deps\" in instantiation of %s, did you mean \"lumesof_deps\"?" % name)
    if legacy_create_init != False:
        fail("Invalid value {legacy_create_init} for attribute 'legacy_create_init', only False is supported.")
    # Transform the arguments
    all_deps = lumesof_deps
    if std_deps:
        all_deps = all_deps + std_deps
    if pip_install:
        all_deps = all_deps + [pip_install]

    # Instantiate py_binary
    py_binary(
        name = name, # Use the name provided to the macro
        srcs = srcs,
        # The 'main' of this py_binary is always our launcher stub.
        # We use $(rlocation) to get its absolute path in the runfiles.
        main = main,
        deps = all_deps,
        data = data,
        visibility = visibility, # Pass the visibility attribute from the macro call
        legacy_create_init = False,
        **kwargs # Forward any other keyword arguments directly to py_binary
    )

def lumesof_py_test(name,
        srcs,
        lumesof_deps = [],
        std_deps = None,
        data = [],
        deps = None,
        main = None,
        pip_install = None,
        visibility = None,
        legacy_create_init = False,
        **kwargs):
    """
    A macro that creates a Python test with integrated pip dependencies.

    Args:
        name: The name of the resulting py_test target. This will be the
              name by which you refer to your final executable (e.g., //app:my_app).
        srcs: List of source files for the Python application's logic. These are
              combined with the launcher stub's source.
        lumesof_deps: List of Bazel dependencies that provide
              LumesofRequirements provider
        std_deps: Standard python dependencies (like py_library, grpc_py_library, etc.)
        data: List of additional data files for the Python application, to be
              included in its runfiles.
        main: The LABEL of the Python script (e.g., "//app:my_app.py") that serves as the
              application's entry point. This label will be used to calculate the
              Python module name for execution by the launcher stub. (Mandatory)
        pip_install: Optional label of a rule (e.g., your `my_pip_install_rule`) that
                  provides pip-installed packages as a single directory within its
                  `DefaultInfo.run_files` depset. If provided, the stub will add
                  these packages to `sys.path`.
        visibility: The visibility of the generated py_test target.
        deps: Must be None to prevent accidental inclusion of legacy dependencies.
        legacy_create_init: Must be false.
        **kwargs: Additional keyword arguments to pass directly to the underlying
                  standard `py_binary` rule (e.g., `args`, `python_version`,
                  `testonly`, `tags`, etc.).
    """
    if deps != None:
        fail("Invalid attribute \"deps\" in instantiation of %s, did you mean \"lumesof_deps\"?" % name)
    if legacy_create_init != False:
        fail("Invalid value {legacy_create_init} for attribute 'legacy_create_init', only False is supported.")
    # Transform the arguments
    all_deps = lumesof_deps
    if std_deps:
        all_deps = all_deps + std_deps
    if pip_install:
        all_deps = all_deps + [pip_install]
    
    # Instantiate py_test
    py_test(
        name = name, # Use the name provided to the macro
        srcs = srcs,
        # The 'main' of this py_binary is always our launcher stub.
        # We use $(rlocation) to get its absolute path in the runfiles.
        main = main,
        deps = all_deps,
        data = data,
        visibility = visibility, # Pass the visibility attribute from the macro call
        legacy_create_init = False,
        **kwargs # Forward any other keyword arguments directly to py_binary
    )

def lumesof_py_library(name,
        deps = None,
        lumesof_deps = None,
        std_deps = None,
        requirements = None,
        imports = [],
        visibility = None,
        **kwargs):

    if deps != None:
        fail("Invalid attribute \"deps\" in instantiation of %s, did you mean \"lumesof_deps\"?" % name)
    internal_pip_req_aggregator = "_lumesof_" + name + "_internal_pip_req_aggregator"
    internal_py_lib_aggregator = "_lumesof_" + name + "_internal_py_lib_aggregator"
 
    all_reqs = []
    if lumesof_deps:
        all_reqs += lumesof_deps
    if requirements:
        all_reqs += requirements
    _lumesof_pip_req(
        name = internal_pip_req_aggregator,
        requirements = all_reqs,
        visibility = ["//visibility:private"],
    )
    
    all_deps = []
    if lumesof_deps:
        all_deps += lumesof_deps
    if std_deps:
        all_deps += std_deps
    py_library(
        name = internal_py_lib_aggregator,
        deps = all_deps,
        imports = imports,
        visibility = ["//visibility:private"],
        **kwargs
    )

    _lumesof_py_library(
        name = name,
        dep = ":" + internal_py_lib_aggregator,
        pip_req = ":" + internal_pip_req_aggregator,
        visibility = visibility,
    )

def lumesof_py_package(
        name,
        dep,
        include_roots = None,
        visibility = None,
        tags = None):
    roots = include_roots if include_roots != None else []
    _lumesof_py_package(
        name = name,
        dep = dep,
        include_roots = roots,
        visibility = visibility,
        tags = tags,
    )

def lumesof_py_wheel(
        name,
        dep,
        distribution,
        version,
        package_roots = None,
        validate_resolve = False,
        visibility = None,
        tags = None,
        **kwargs):
    internal_wheel_package = "_lumesof_{}_internal_wheel_package".format(name)
    internal_wheel_requirements = "_lumesof_{}_internal_wheel_requirements".format(name)
    strip_path_prefixes = kwargs.pop("strip_path_prefixes", [])
    package_name = native.package_name()
    internal_package_prefix = ((package_name + "/") if package_name else "") + internal_wheel_package + ".pkg"
    wheel_strip_path_prefixes = [internal_package_prefix] + strip_path_prefixes

    _lumesof_py_package(
        name = internal_wheel_package,
        dep = dep,
        include_roots = package_roots if package_roots != None else [],
        visibility = ["//visibility:private"],
        tags = tags,
    )

    _lumesof_wheel_requirements(
        name = internal_wheel_requirements,
        dep = dep,
        validate_resolve = validate_resolve,
        visibility = ["//visibility:private"],
        tags = tags,
    )

    py_wheel(
        name = name,
        distribution = distribution,
        version = version,
        deps = [":" + internal_wheel_package],
        requires_file = ":" + internal_wheel_requirements,
        strip_path_prefixes = wheel_strip_path_prefixes,
        visibility = visibility,
        tags = tags,
        **kwargs
    )
