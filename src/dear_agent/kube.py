from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
HOST = "https://kubernetes.default.svc"


class KubernetesError(RuntimeError):
    """A request to the Kubernetes API failed."""


@dataclass(slots=True)
class KubernetesClient:
    """A tiny in-cluster Kubernetes API client (stdlib only).

    Dear Agent's dispatcher must create one Job per task, which means talking to the API. Rather
    than ship `kubectl`/`oc` in the image, this uses the mounted service-account token and
    CA. It is deliberately minimal: read a ConfigMap, create a Job, nothing else.
    """

    namespace: str
    token: str
    ca_path: Path = SA_DIR / "ca.crt"
    host: str = HOST
    timeout: float = 30.0

    @classmethod
    def from_cluster(cls, namespace: str = "", *, sa_dir: Path = SA_DIR) -> KubernetesClient:
        token_path = sa_dir / "token"
        if not token_path.exists():
            raise KubernetesError(f"no service-account token at {token_path}")
        # The pod's own namespace is authoritative: the dispatcher creates runner Jobs where
        # it runs, whatever namespace an operator configured elsewhere.
        namespace_file = sa_dir / "namespace"
        if namespace_file.exists():
            namespace = namespace_file.read_text().strip()
        return cls(
            namespace=namespace, token=token_path.read_text().strip(), ca_path=sa_dir / "ca.crt"
        )

    def get_configmap(self, name: str) -> dict[str, str]:
        body = self._request("GET", f"/api/v1/namespaces/{self.namespace}/configmaps/{name}")
        data = body.get("data", {})
        if not isinstance(data, dict):
            raise KubernetesError(f"configmap {name!r} has no data")
        return {str(key): str(value) for key, value in data.items()}

    def create_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Create a Job. A Job that already exists is success (idempotent dispatch)."""
        path = f"/apis/batch/v1/namespaces/{self.namespace}/jobs"
        try:
            return self._request("POST", path, payload=job)
        except KubernetesError as exc:
            if " 409 " in f" {exc} " or "AlreadyExists" in str(exc):
                name = job.get("metadata", {}).get("name", "")
                return self._request("GET", f"{path}/{name}")
            raise

    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.host}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._context()
            ) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise KubernetesError(f"{method} {path} failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise KubernetesError(f"{method} {path} failed: {exc.reason}") from exc

    def _context(self) -> ssl.SSLContext | None:
        if self.host.startswith("http://") or not self.ca_path.exists():
            return None
        return ssl.create_default_context(cafile=str(self.ca_path))


__all__ = ["KubernetesClient", "KubernetesError"]
