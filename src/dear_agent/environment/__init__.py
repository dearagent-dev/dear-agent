"""Execution environments (ADR 0008).

An environment is what the repository declares; the harness is *injected* into it, never
assumed to be present. This package holds the injection mechanisms.
"""

from dear_agent.environment.bundle import (
    OPENCODE_FILES,
    BundleFile,
    HarnessBundle,
    default_bundle_cache_dir,
    opencode_bundle,
)

__all__ = [
    "OPENCODE_FILES",
    "BundleFile",
    "HarnessBundle",
    "default_bundle_cache_dir",
    "opencode_bundle",
]
