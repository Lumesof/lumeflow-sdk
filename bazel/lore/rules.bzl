"""LORe build/publish rules.

Canonical LORe rule entrypoint.
"""

load("@rules_oci//oci:defs.bzl", "oci_push")
load("@rules_python//python:defs.bzl", "PyInfo")
load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")
load("//bazel/python:rules.bzl", "lumesof_py_binary", "lumesof_py_library")
load(
    "//bazel/pkg:shared_rules_impl.bzl",
    _shared_lumesof_oci_image = "lumesof_oci_image",
    _shared_lumesof_repo = "lumesof_repo",
)

def _as_label(value):
    if type(value) == "Label":
        return value
    if type(value) != "string":
        fail("Expected label string or Label, got: %s" % type(value))
    if value.startswith("@") or value.startswith("//"):
        return Label(value)
    return native.package_relative_label(value)

def _with_target_suffix(value, suffix):
    return str(_as_label(value)) + suffix

def _lore_repo(publisher, slug):
    return "lore-registry/%s/%s" % (publisher, slug)

def _lore_e2e_default_repo(slug):
    prefix = native.package_name().replace("/", "-")
    return "%s-%s-lore" % (prefix, slug)

def _lore_validate_non_empty(name, value):
    if type(value) != "string" or not value:
        fail("%s must be a non-empty string" % name)

def _lore_validate_semver_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".ok")

    ctx.actions.run_shell(
        tools = [ctx.executable._validator],
        outputs = [out],
        command = "\"$1\" \"$2\" && printf 'ok\\n' > \"$3\"",
        arguments = [
            ctx.executable._validator.path,
            ctx.attr.version,
            out.path,
        ],
        mnemonic = "LoreValidateSemver",
        progress_message = "Validating semver for %s" % ctx.label,
    )

    return [DefaultInfo(files = depset([out]))]

_lore_validate_semver = rule(
    implementation = _lore_validate_semver_impl,
    attrs = {
        "version": attr.string(mandatory = True),
        "_validator": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//bazel/lore:validate_semver"),
        ),
    },
)

def _lore_extract_ports_impl(ctx):
    operator_files = [
        file
        for file in ctx.attr.operator_src[DefaultInfo].files.to_list()
        if file.path.endswith(".py")
    ]
    if len(operator_files) != 1:
        fail(
            "operator_src %s must expose exactly one .py source file, found %d" % (
                ctx.attr.operator_src.label,
                len(operator_files),
            ),
        )

    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.run(
        executable = ctx.executable.extractor,
        inputs = [
            ctx.file.semver_marker,
            operator_files[0],
        ],
        tools = [ctx.executable.extractor],
        outputs = [out],
        arguments = [
            "--source",
            operator_files[0].path,
            "--class",
            ctx.attr.operator_class,
            "--output",
            out.path,
        ],
        mnemonic = "LoreExtractPorts",
        progress_message = "Extracting LORe ports for %s" % ctx.label,
    )

    return [DefaultInfo(files = depset([out]))]

_lore_extract_ports = rule(
    implementation = _lore_extract_ports_impl,
    attrs = {
        "operator_src": attr.label(mandatory = True),
        "operator_class": attr.string(mandatory = True),
        "semver_marker": attr.label(allow_single_file = True, mandatory = True),
        "extractor": attr.label(
            executable = True,
            cfg = "exec",
            mandatory = True,
        ),
    },
)

def _lore_extract_graph_impl(ctx):
    graph_files = [
        file
        for file in ctx.attr.graph[DefaultInfo].files.to_list()
        if file.path.endswith(".py")
    ]
    if len(graph_files) != 1:
        fail(
            "graph %s must expose exactly one .py source file, found %d" % (
                ctx.attr.graph.label,
                len(graph_files),
            ),
        )

    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.run(
        executable = ctx.executable.extractor,
        inputs = [
            graph_files[0],
        ],
        tools = [ctx.executable.extractor],
        outputs = [out],
        arguments = [
            "--source",
            graph_files[0].path,
            "--class",
            ctx.attr.graph_class,
            "--output",
            out.path,
        ],
        mnemonic = "LoreExtractGraph",
        progress_message = "Extracting LORe graph metadata for %s" % ctx.label,
    )

    return [DefaultInfo(files = depset([out]))]

_lore_extract_graph = rule(
    implementation = _lore_extract_graph_impl,
    attrs = {
        "graph": attr.label(mandatory = True, allow_single_file = True),
        "graph_class": attr.string(mandatory = True),
        "extractor": attr.label(
            executable = True,
            cfg = "exec",
            mandatory = True,
        ),
    },
)

def _lore_build_labels_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".labels.txt")

    ctx.actions.run(
        executable = ctx.executable._label_builder,
        inputs = [
            ctx.file.ports,
            ctx.file.semver_marker,
        ],
        tools = [ctx.executable._label_builder],
        outputs = [out],
        arguments = [
            "--ports-json",
            ctx.file.ports.path,
            "--output",
            out.path,
            "--publisher",
            ctx.attr.publisher,
            "--slug",
            ctx.attr.slug,
            "--description",
            ctx.attr.description,
            "--version",
            ctx.attr.version,
            "--category",
            ctx.attr.category,
            "--visibility",
            ctx.attr.operator_visibility,
            "--lumeflow-min-version",
            ctx.attr.lumeflow_min_version,
            "--changelog",
            ctx.attr.changelog,
        ],
        mnemonic = "LoreBuildLabels",
        progress_message = "Building LORe OCI labels for %s" % ctx.label,
    )

    return [DefaultInfo(files = depset([out]))]

_lore_build_labels = rule(
    implementation = _lore_build_labels_impl,
    attrs = {
        "ports": attr.label(allow_single_file = True, mandatory = True),
        "semver_marker": attr.label(allow_single_file = True, mandatory = True),
        "publisher": attr.string(mandatory = True),
        "slug": attr.string(mandatory = True),
        "description": attr.string(mandatory = True),
        "version": attr.string(mandatory = True),
        "category": attr.string(mandatory = True),
        "operator_visibility": attr.string(mandatory = True),
        "lumeflow_min_version": attr.string(mandatory = True),
        "changelog": attr.string(mandatory = True),
        "_label_builder": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//bazel/lore:build_labels"),
        ),
    },
)

def _default_lore_image_descriptor_import_path(binary):
    binary_label = _as_label(binary)
    package_name = binary_label.package
    base_name = binary_label.name + "_impage_descriptor"
    if package_name:
        return package_name.replace("/", ".") + "." + base_name
    return base_name

def _default_lore_graph_publisher_import_path(name):
    package_name = native.package_name()
    base_name = name + "_publisher"
    if package_name:
        return package_name.replace("/", ".") + "." + base_name
    return base_name

OperatorInfo = provider(fields = [
    "ports_manifest",
    "upload_repository",
    "upload_repository_path",
    "remote_tags",
    "publisher",
    "slug",
    "description",
    "category",
    "version",
    "operator_visibility",
    "lumeflow_min_version",
    "changelog",
    "image_digest_file",
])

LoreGraphInfo = provider(fields = [
    "graph_manifest",
    "graph_class",
    "operators",
    "operator_publishers",
])

def _lore_prepare_operator_targets(
        name,
        binary,
        operator_src,
        operator_class,
        publisher,
        slug,
        description,
        category,
        version,
        visibility,
        use_default_registry = False,
        tags = None,
        lumeflow_min_version = None,
        changelog = None,
        oci_base = None):
    if visibility not in ("public", "private"):
        fail("visibility must be 'public' or 'private', got %r" % visibility)

    for field_name, field_value in [
        ("publisher", publisher),
        ("slug", slug),
        ("description", description),
        ("category", category),
        ("version", version),
        ("operator_class", operator_class),
    ]:
        _lore_validate_non_empty(field_name, field_value)

    normalized_lumeflow_min_version = lumeflow_min_version if lumeflow_min_version != None else ""
    normalized_changelog = changelog if changelog != None else ""
    normalized_tags = tags if tags != None else ["manual"]
    publish_repo = _lore_e2e_default_repo(slug) if use_default_registry else _lore_repo(publisher, slug)
    publish_repo_url = _shared_lumesof_repo(publish_repo)

    extractor_tool = name + "__extract_ports_tool"
    lumesof_py_binary(
        name = extractor_tool,
        srcs = ["//bazel/lore:extract_ports.py"],
        main = "//bazel/lore:extract_ports.py",
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

    version_ok = name + "__version_ok"
    _lore_validate_semver(
        name = version_ok,
        version = version,
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

    ports_target = name + "__ports"
    _lore_extract_ports(
        name = ports_target,
        operator_src = operator_src,
        operator_class = operator_class,
        semver_marker = ":" + version_ok,
        extractor = ":" + extractor_tool,
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

    labels_target = name + "__labels"
    _lore_build_labels(
        name = labels_target,
        ports = ":" + ports_target,
        semver_marker = ":" + version_ok,
        publisher = publisher,
        slug = slug,
        description = description,
        version = version,
        category = category,
        operator_visibility = visibility,
        lumeflow_min_version = normalized_lumeflow_min_version,
        changelog = normalized_changelog,
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

    image_target = name + "__image"
    image_kwargs = {
        "name": image_target,
        "binary": binary,
        "labels": ":" + labels_target,
    }
    if oci_base != None:
        image_kwargs["base"] = oci_base
    _shared_lumesof_oci_image(**image_kwargs)

    digest_target = name + "__image_digest"
    native.filegroup(
        name = digest_target,
        srcs = [":" + image_target],
        output_group = "digest",
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

    return struct(
        image = ":" + image_target,
        ports = ":" + ports_target,
        digest_file = ":" + digest_target,
        publish_repo = publish_repo_url,
        publish_repo_path = publish_repo,
        tags = normalized_tags,
        publisher = publisher,
        slug = slug,
        description = description,
        category = category,
        version = version,
        operator_visibility = visibility,
        lumeflow_min_version = normalized_lumeflow_min_version,
        changelog = normalized_changelog,
    )

def _lore_operator_bundle_impl(ctx):
    module_path = ctx.attr.module_path
    if not module_path:
        fail("module_path must be non-empty")

    package_module_path = ctx.label.package.replace("/", ".")
    expected_prefix = package_module_path + "."
    if not module_path.startswith(expected_prefix):
        fail(
            "module_path must be under package %r (expected prefix %r), got %r" % (
                ctx.label.package,
                expected_prefix,
                module_path,
            )
        )

    relative_module_path = module_path[len(expected_prefix):]
    path_segments = relative_module_path.split(".")
    for segment in path_segments:
        if not segment:
            fail("module_path contains an empty segment: %r" % module_path)

    output_relative_path = "/".join(path_segments) + ".py"
    py_out = ctx.actions.declare_file(output_relative_path)

    ctx.actions.run(
        executable = ctx.executable._generator,
        inputs = [
            ctx.file.ports_manifest,
            ctx.file.digest_file,
        ],
        tools = [ctx.executable._generator],
        outputs = [py_out],
        arguments = [
            "--ports-json",
            ctx.file.ports_manifest.path,
            "--digest-file",
            ctx.file.digest_file.path,
            "--image-repository",
            ctx.attr.upload_repository,
            "--module-path",
            module_path,
            "--output",
            py_out.path,
        ],
        mnemonic = "LoreGenerateImageDescriptorPy",
        progress_message = "Generating operator image descriptor module for %s" % ctx.label,
    )

    pyinfo = PyInfo(
        transitive_sources = depset([py_out]),
        imports = depset([]),
        has_py2_only_sources = False,
        has_py3_only_sources = True,
    )

    image_default_info = ctx.attr.image[DefaultInfo]
    runfiles = ctx.runfiles(files = [py_out])
    if image_default_info.default_runfiles:
        runfiles = runfiles.merge(image_default_info.default_runfiles)
    if image_default_info.data_runfiles:
        runfiles = runfiles.merge(image_default_info.data_runfiles)

    return [
        DefaultInfo(
            files = image_default_info.files,
            runfiles = runfiles,
        ),
        pyinfo,
        OperatorInfo(
            ports_manifest = ctx.file.ports_manifest,
            upload_repository = ctx.attr.upload_repository,
            upload_repository_path = ctx.attr.upload_repository_path,
            remote_tags = tuple(ctx.attr.remote_tags),
            publisher = ctx.attr.publisher,
            slug = ctx.attr.slug,
            description = ctx.attr.description,
            category = ctx.attr.category,
            version = ctx.attr.version,
            operator_visibility = ctx.attr.operator_visibility,
            lumeflow_min_version = ctx.attr.lumeflow_min_version,
            changelog = ctx.attr.changelog,
            image_digest_file = ctx.file.digest_file,
        ),
    ]

_lore_operator_bundle = rule(
    implementation = _lore_operator_bundle_impl,
    attrs = {
        "image": attr.label(
            mandatory = True,
        ),
        "ports_manifest": attr.label(
            mandatory = True,
            allow_single_file = True,
        ),
        "digest_file": attr.label(
            mandatory = True,
            allow_single_file = True,
        ),
        "upload_repository": attr.string(mandatory = True),
        "upload_repository_path": attr.string(mandatory = True),
        "remote_tags": attr.string_list(mandatory = True),
        "publisher": attr.string(mandatory = True),
        "slug": attr.string(mandatory = True),
        "description": attr.string(mandatory = True),
        "category": attr.string(mandatory = True),
        "version": attr.string(mandatory = True),
        "operator_visibility": attr.string(mandatory = True),
        "lumeflow_min_version": attr.string(mandatory = True),
        "changelog": attr.string(mandatory = True),
        "module_path": attr.string(mandatory = True),
        "_generator": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//bazel/lore:generate_image_descriptor_lib"),
        ),
    },
)

def _lore_operator_repository_file_impl(ctx):
    info = ctx.attr.operator[OperatorInfo]
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

    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.write(
        output = out,
        content = "%s/%s\n" % (repo_prefix, info.upload_repository_path),
    )
    return [DefaultInfo(files = depset([out]))]

_lore_operator_repository_file = rule(
    implementation = _lore_operator_repository_file_impl,
    attrs = {
        "operator": attr.label(
            mandatory = True,
            providers = [OperatorInfo],
        ),
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

def _lore_operator_remote_tags_file_impl(ctx):
    info = ctx.attr.operator[OperatorInfo]
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.write(
        output = out,
        content = "\n".join(info.remote_tags) + "\n",
    )
    return [DefaultInfo(files = depset([out]))]

_lore_operator_remote_tags_file = rule(
    implementation = _lore_operator_remote_tags_file_impl,
    attrs = {
        "operator": attr.label(
            mandatory = True,
            providers = [OperatorInfo],
        ),
    },
)

def lore_operator(
        name,
        binary,
        operator_src,
        operator_class,
        publisher,
        slug,
        description,
        category,
        version,
        visibility,
        use_default_registry = False,
        tags = None,
        lumeflow_min_version = None,
        changelog = None,
        python_import_path = None,
        oci_base = None,
        target_visibility = None):
    targets = _lore_prepare_operator_targets(
        name = name,
        binary = binary,
        operator_src = operator_src,
        operator_class = operator_class,
        publisher = publisher,
        slug = slug,
        description = description,
        category = category,
        version = version,
        visibility = visibility,
        use_default_registry = use_default_registry,
        tags = tags,
        lumeflow_min_version = lumeflow_min_version,
        changelog = changelog,
        oci_base = oci_base,
    )

    normalized_python_import_path = python_import_path
    if normalized_python_import_path == None:
        normalized_python_import_path = _default_lore_image_descriptor_import_path(binary)

    normalized_target_visibility = target_visibility
    if normalized_target_visibility == None:
        normalized_target_visibility = ["//visibility:public"]

    _lore_operator_bundle(
        name = name,
        image = targets.image,
        ports_manifest = targets.ports,
        digest_file = targets.digest_file,
        upload_repository = targets.publish_repo,
        upload_repository_path = targets.publish_repo_path,
        remote_tags = [targets.version],
        publisher = targets.publisher,
        slug = targets.slug,
        description = targets.description,
        category = targets.category,
        version = targets.version,
        operator_visibility = targets.operator_visibility,
        lumeflow_min_version = targets.lumeflow_min_version,
        changelog = targets.changelog,
        module_path = normalized_python_import_path,
        tags = targets.tags,
        visibility = normalized_target_visibility,
    )

def lore_operator_publish(
        name,
        operator,
        tags = None):
    normalized_tags = tags if tags != None else ["manual"]

    repository_target = name + "__repository"
    _lore_operator_repository_file(
        name = repository_target,
        operator = operator,
        visibility = ["//visibility:private"],
        tags = normalized_tags,
    )

    remote_tags_target = name + "__remote_tags"
    _lore_operator_remote_tags_file(
        name = remote_tags_target,
        operator = operator,
        visibility = ["//visibility:private"],
        tags = normalized_tags,
    )

    oci_push(
        name = name,
        image = operator,
        repository_file = ":" + repository_target,
        remote_tags = ":" + remote_tags_target,
        tags = normalized_tags,
    )

def _lore_graph_info_impl(ctx):
    for publisher in ctx.attr.operator_publishers:
        files_to_run = publisher[DefaultInfo].files_to_run
        if files_to_run == None or files_to_run.executable == None:
            fail(
                "operator publisher target %s must be executable" % publisher.label
            )

    return [
        DefaultInfo(files = depset([ctx.file.graph_manifest])),
        LoreGraphInfo(
            graph_manifest = ctx.file.graph_manifest,
            graph_class = ctx.attr.graph_class,
            operators = tuple(ctx.attr.operators),
            operator_publishers = tuple(ctx.attr.operator_publishers),
        ),
    ]

_lore_graph_info = rule(
    implementation = _lore_graph_info_impl,
    attrs = {
        "graph_manifest": attr.label(
            mandatory = True,
            allow_single_file = True,
        ),
        "graph_class": attr.string(mandatory = True),
        "operators": attr.label_list(
            providers = [OperatorInfo],
        ),
        "operator_publishers": attr.label_list(
        ),
    },
)

def _lore_graph_publisher_module_impl(ctx):
    module_path = ctx.attr.module_path
    if not module_path:
        fail("module_path must be non-empty")

    package_module_path = ctx.label.package.replace("/", ".")
    expected_prefix = package_module_path + "."
    if not module_path.startswith(expected_prefix):
        fail(
            "module_path must be under package %r (expected prefix %r), got %r" % (
                ctx.label.package,
                expected_prefix,
                module_path,
            )
        )

    relative_module_path = module_path[len(expected_prefix):]
    path_segments = relative_module_path.split(".")
    for segment in path_segments:
        if not segment:
            fail("module_path contains an empty segment: %r" % module_path)

    publisher_paths = []
    runfiles = ctx.runfiles()
    for publisher in ctx.attr.publishers:
        default_info = publisher[DefaultInfo]
        files_to_run = default_info.files_to_run
        if files_to_run == None or files_to_run.executable == None:
            fail(
                "publisher target %s must be executable" % publisher.label
            )
        publisher_paths.append(files_to_run.executable.short_path)
        runfiles = runfiles.merge(ctx.runfiles(files = [files_to_run.executable]))
        if default_info.default_runfiles:
            runfiles = runfiles.merge(default_info.default_runfiles)
        if default_info.data_runfiles:
            runfiles = runfiles.merge(default_info.data_runfiles)

    output_relative_path = "/".join(path_segments) + ".py"
    py_out = ctx.actions.declare_file(output_relative_path)

    args = ctx.actions.args()
    args.add("--module-path", module_path)
    args.add("--workspace-name", ctx.workspace_name)
    for publisher_path in publisher_paths:
        args.add("--publisher-path", publisher_path)
    args.add("--output", py_out.path)

    ctx.actions.run(
        executable = ctx.executable._generator,
        outputs = [py_out],
        arguments = [args],
        tools = [ctx.executable._generator],
        mnemonic = "LoreGenerateGraphPublisherPy",
        progress_message = "Generating graph publisher module for %s" % ctx.label,
    )

    base_default_info = ctx.attr._base_publisher_dep[DefaultInfo]
    if base_default_info.default_runfiles:
        runfiles = runfiles.merge(base_default_info.default_runfiles)
    if base_default_info.data_runfiles:
        runfiles = runfiles.merge(base_default_info.data_runfiles)
    runfiles = runfiles.merge(ctx.runfiles(files = [py_out]))

    base_py_info = ctx.attr._base_publisher_dep[PyInfo]
    pyinfo = PyInfo(
        transitive_sources = depset([py_out], transitive = [base_py_info.transitive_sources]),
        imports = depset(transitive = [base_py_info.imports]),
        has_py2_only_sources = False,
        has_py3_only_sources = True,
    )

    return [
        DefaultInfo(
            files = depset([py_out]),
            runfiles = runfiles,
        ),
        pyinfo,
    ]

_lore_graph_publisher_module = rule(
    implementation = _lore_graph_publisher_module_impl,
    attrs = {
        "module_path": attr.string(mandatory = True),
        "publishers": attr.label_list(),
        "_generator": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//bazel/lore:generate_graph_publisher_lib"),
        ),
        "_base_publisher_dep": attr.label(
            default = Label("//bazel/lore:base_publisher_lib"),
            providers = [PyInfo],
        ),
    },
)

def _lore_graph_publish_impl(ctx):
    info = ctx.attr.graph_info[LoreGraphInfo]
    runfiles = ctx.runfiles(files = [info.graph_manifest])
    workspace_name = ctx.workspace_name
    publisher_paths = []
    for publisher in info.operator_publishers:
        default_info = publisher[DefaultInfo]
        files_to_run = default_info.files_to_run
        if files_to_run == None or files_to_run.executable == None:
            fail(
                "operator publisher target %s must be executable" % publisher.label
            )
        publisher_paths.append(files_to_run.executable.short_path)
        runfiles = runfiles.merge(ctx.runfiles(files = [files_to_run.executable]))
        if default_info.default_runfiles:
            runfiles = runfiles.merge(default_info.default_runfiles)
        if default_info.data_runfiles:
            runfiles = runfiles.merge(default_info.data_runfiles)

    runner = ctx.actions.declare_file(ctx.label.name + ".sh")
    script_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "if [[ -n \"${RUNFILES_DIR:-}\" ]]; then",
        "  RUNFILES_ROOT=\"${RUNFILES_DIR}\"",
        "elif [[ -n \"${TEST_SRCDIR:-}\" ]]; then",
        "  RUNFILES_ROOT=\"${TEST_SRCDIR}\"",
        "elif [[ -d \"$0.runfiles\" ]]; then",
        "  RUNFILES_ROOT=\"$0.runfiles\"",
        "else",
        "  echo \"unable to locate runfiles directory\" >&2",
        "  exit 1",
        "fi",
        "",
        "MAIN_WORKSPACE_NAME=\"%s\"" % workspace_name,
        "",
        "runPublisher() {",
        "  local relative_path=\"$1\"",
        "  local executable_path=\"\"",
        "  local candidate=\"\"",
        "  local -a candidates=(",
        "    \"$RUNFILES_ROOT/$relative_path\"",
        "    \"$RUNFILES_ROOT/${MAIN_WORKSPACE_NAME}/$relative_path\"",
        "    \"$RUNFILES_ROOT/_main/$relative_path\"",
        "  )",
        "  for candidate in \"${candidates[@]}\"; do",
        "    if [[ -x \"$candidate\" ]]; then",
        "      executable_path=\"$candidate\"",
        "      break",
        "    fi",
        "  done",
        "  if [[ -z \"$executable_path\" ]]; then",
        "    echo \"publisher executable missing or not executable: $RUNFILES_ROOT/$relative_path\" >&2",
        "    exit 1",
        "  fi",
        "  RUNFILES_DIR=\"$RUNFILES_ROOT\" \"$executable_path\"",
        "}",
        "",
    ]
    for path in publisher_paths:
        script_lines.append("runPublisher \"%s\"" % path)
    script_lines.append("")

    ctx.actions.write(
        output = runner,
        content = "\n".join(script_lines),
        is_executable = True,
    )

    return [
        DefaultInfo(
            executable = runner,
            runfiles = runfiles,
            files = depset([runner]),
        ),
    ]

_lore_graph_publish_runner = rule(
    implementation = _lore_graph_publish_impl,
    attrs = {
        "graph_info": attr.label(
            mandatory = True,
            providers = [LoreGraphInfo],
        ),
    },
    executable = True,
)

def lore_graph(
        name,
        graph,
        graph_class,
        operators,
        publisher_python_import_path = None,
        srcs = None,
        requirements = None,
        lumesof_deps = None,
        std_deps = None,
        visibility = None,
        target_visibility = None,
        tags = None,
        **kwargs):
    normalized_tags = tags if tags != None else ["manual"]
    normalized_visibility = target_visibility if target_visibility != None else visibility
    if normalized_visibility == None:
        normalized_visibility = ["//visibility:public"]

    normalized_srcs = [graph]
    if srcs != None:
        normalized_srcs.extend(srcs)

    normalized_lumesof_deps = []
    if lumesof_deps != None:
        normalized_lumesof_deps.extend(lumesof_deps)

    normalized_std_deps = []
    if std_deps != None:
        normalized_std_deps.extend(std_deps)
    normalized_std_deps.extend(operators)

    lumesof_py_library(
        name = name,
        srcs = normalized_srcs,
        requirements = requirements,
        lumesof_deps = normalized_lumesof_deps,
        std_deps = normalized_std_deps,
        visibility = normalized_visibility,
        tags = normalized_tags,
        **kwargs
    )

    extractor_tool = name + "__extract_graph_tool"
    lumesof_py_binary(
        name = extractor_tool,
        srcs = ["//bazel/lore:extract_graph.py"],
        main = "//bazel/lore:extract_graph.py",
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

    graph_manifest_target = name + "__graph_manifest"
    _lore_extract_graph(
        name = graph_manifest_target,
        graph = graph,
        graph_class = graph_class,
        extractor = ":" + extractor_tool,
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

    operator_publish_targets = []
    for index, operator in enumerate(operators):
        publish_target = "%s__operator_publish_%d" % (name, index)
        lore_operator_publish(
            name = publish_target,
            operator = operator,
            tags = normalized_tags,
        )
        operator_publish_targets.append(":" + publish_target)

    normalized_publisher_import_path = publisher_python_import_path
    if normalized_publisher_import_path == None:
        normalized_publisher_import_path = _default_lore_graph_publisher_import_path(name)

    _lore_graph_publisher_module(
        name = name + "_publisher",
        module_path = normalized_publisher_import_path,
        publishers = operator_publish_targets,
        tags = normalized_tags,
        visibility = normalized_visibility,
    )

    _lore_graph_info(
        name = name + "__lore_graph_info",
        graph_manifest = ":" + graph_manifest_target,
        graph_class = graph_class,
        operators = operators,
        operator_publishers = operator_publish_targets,
        tags = normalized_tags,
        visibility = ["//visibility:private"],
    )

def lore_graph_publish(
        name,
        graph,
        tags = None,
        visibility = None):
    normalized_tags = tags if tags != None else ["manual"]
    _lore_graph_publish_runner(
        name = name,
        graph_info = _with_target_suffix(graph, "__lore_graph_info"),
        tags = normalized_tags,
        visibility = visibility,
    )
