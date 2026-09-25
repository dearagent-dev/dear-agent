from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dear_agent.sandbox import Mount

DEFAULT_CONTAINER_BINARY = "podman"
DEFAULT_BUNDLE_DIR = "/opt/dear-agent/harness"
DEFAULT_BIN_DIR = "/usr/local/bin"
_READY = ".ready"

Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _podman(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass(frozen=True, slots=True)
class BundleFile:
    """One file copied out of the harness image, and the name it takes in the bundle."""

    source: str
    name: str


# OpenCode ships a musl-linked binary, so a glibc environment cannot run it directly. Bundling
# the binary with its musl loader and its shared libraries (named by soname) makes it portable:
# the shim invokes the bundled loader, which resolves the libraries from the bundle directory.
OPENCODE_FILES: tuple[BundleFile, ...] = (
    BundleFile("/usr/local/bin/opencode", "opencode"),
    BundleFile("/lib/ld-musl-x86_64.so.1", "ld-musl-x86_64.so.1"),
    BundleFile("/usr/lib/libstdc++.so.6", "libstdc++.so.6"),
    BundleFile("/usr/lib/libgcc_s.so.1", "libgcc_s.so.1"),
)


@dataclass(slots=True)
class HarnessBundle:
    """A portable copy of a harness binary plus the runtime it needs to run anywhere.

    The harness's own image is its distribution channel (ADR 0006), but the environment image a
    task runs in (a dev container, an Ansible EE) does not contain the harness. This materializes
    the binary and its dynamic loader/libraries into a cache directory that any Linux base can
    mount and run — no build and no network at run time (ADR 0008). A small shim on ``PATH``
    invokes the binary through the bundled loader, so a musl-linked harness runs in a glibc
    environment and vice versa.
    """

    image: str
    files: tuple[BundleFile, ...]
    interpreter: str
    binary: str
    cache_dir: Path
    container_binary: str = DEFAULT_CONTAINER_BINARY
    bundle_dir: str = DEFAULT_BUNDLE_DIR
    bin_dir: str = DEFAULT_BIN_DIR
    _run: Run = field(default=_podman, repr=False)

    @property
    def ready(self) -> bool:
        return (self.cache_dir / _READY).exists()

    @property
    def root(self) -> Path:
        return self.cache_dir / "root"

    @property
    def shim(self) -> Path:
        return self.cache_dir / "bin" / self.binary

    def ensure(self) -> None:
        """Materialize the bundle if it is not already cached. Idempotent."""
        if self.ready:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.shim.parent.mkdir(parents=True, exist_ok=True)
        created = self._run([self.container_binary, "create", self.image])
        if created.returncode != 0:
            raise RuntimeError(created.stderr.strip() or f"cannot create {self.image!r}")
        container = created.stdout.strip()
        try:
            for item in self.files:
                result = self._run(
                    [
                        self.container_binary,
                        "cp",
                        f"{container}:{item.source}",
                        str(self.root / item.name),
                    ]
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or f"cannot copy {item.source!r}")
        finally:
            self._run([self.container_binary, "rm", "-f", container])
        self._write_shim()
        (self.cache_dir / _READY).write_text("ok\n")

    def _write_shim(self) -> None:
        self.shim.write_text(
            "#!/bin/sh\n"
            f"LD_LIBRARY_PATH={self.bundle_dir} "
            f"exec {self.bundle_dir}/{self.interpreter} "
            f'{self.bundle_dir}/{self.binary} "$@"\n'
        )
        self.shim.chmod(self.shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def mounts(self) -> tuple[Mount, ...]:
        """The mounts that expose the bundle inside an environment session.

        Marked ``relabel`` because the cache is Dear Agent's own; unlike the operator's
        credential mounts, relabeling it on SELinux is safe and lets the mounted shim run
        without requiring ``DEAR_AGENT_HARNESS_CONTAINER_SELINUX=Z``.
        """
        return (
            Mount(str(self.root), self.bundle_dir, readonly=True, relabel=True),
            Mount(str(self.shim), f"{self.bin_dir}/{self.binary}", readonly=True, relabel=True),
        )


def default_bundle_cache_dir() -> Path:
    """Where harness bundles are cached across runs (``DEAR_AGENT_HARNESS_BUNDLE_CACHE``)."""
    root = os.environ.get("DEAR_AGENT_HARNESS_BUNDLE_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "dear-agent", "harness"
    )
    return Path(root)


def opencode_bundle(
    image: str,
    *,
    cache_dir: Path | None = None,
    container_binary: str = DEFAULT_CONTAINER_BINARY,
) -> HarnessBundle:
    """The OpenCode bundle recipe (musl binary + loader + ``libstdc++``/``libgcc``)."""
    return HarnessBundle(
        image=image,
        files=OPENCODE_FILES,
        interpreter="ld-musl-x86_64.so.1",
        binary="opencode",
        cache_dir=cache_dir or default_bundle_cache_dir() / "opencode",
        container_binary=container_binary,
    )


__all__ = [
    "DEFAULT_BIN_DIR",
    "DEFAULT_BUNDLE_DIR",
    "DEFAULT_CONTAINER_BINARY",
    "OPENCODE_FILES",
    "BundleFile",
    "HarnessBundle",
    "default_bundle_cache_dir",
    "opencode_bundle",
]
