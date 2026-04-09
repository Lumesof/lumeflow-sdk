"""Public façade for Lumesof packaging rules.

Customer-facing rules are sourced from a shared implementation module under
`bazel/pkg/` so internal and external consumers use the same code path.
"""

load(
    "//bazel/pkg:shared_rules_impl.bzl",
    _lumesof_app_image = "lumesof_app_image",
    _lumesof_common_extra_sonames = "lumesof_common_extra_sonames",
    _lumesof_oci_image = "lumesof_oci_image",
    _lumesof_oci_image_publish = "lumesof_oci_image_publish",
    _lumesof_oci_local_executor = "lumesof_oci_local_executor",
)

lumesof_oci_image = _lumesof_oci_image
lumesof_oci_image_publish = _lumesof_oci_image_publish
lumesof_oci_local_executor = _lumesof_oci_local_executor
lumesof_app_image = _lumesof_app_image
lumesof_common_extra_sonames = _lumesof_common_extra_sonames
