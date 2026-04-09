"""Shared archive-path provider and helper rule."""

LumesofArchivePathsInfo = provider(
    doc = "Archive paths to package into deterministic tar artifacts.",
    fields = {
        "archive_paths": "depset of absolute runtime search paths to archive.",
    },
)

def _lumesof_archive_paths_info_impl(ctx):
    return [LumesofArchivePathsInfo(
        archive_paths = depset(ctx.attr.archive_paths),
    )]

lumesof_archive_paths_info = rule(
    implementation = _lumesof_archive_paths_info_impl,
    attrs = {
        "archive_paths": attr.string_list(mandatory = True),
    },
)
