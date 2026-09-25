"""Execution environments (ADR 0008).

An environment is what the repository declares; the harness is *injected* into it, never
assumed to be present. This package holds the descriptor resolver and the injection mechanisms.
"""

from dear_agent.environment.builder import (
    DEFAULT_IMAGE_NAME,
    EnvironmentBuilder,
    EnvironmentBuildError,
)
from dear_agent.environment.bundle import (
    OPENCODE_FILES,
    BundleFile,
    HarnessBundle,
    default_bundle_cache_dir,
    opencode_bundle,
)
from dear_agent.environment.descriptor import EnvironmentDescriptor, detect

__all__ = [
    "DEFAULT_IMAGE_NAME",
    "OPENCODE_FILES",
    "BundleFile",
    "EnvironmentBuildError",
    "EnvironmentBuilder",
    "EnvironmentDescriptor",
    "HarnessBundle",
    "default_bundle_cache_dir",
    "detect",
    "opencode_bundle",
]
