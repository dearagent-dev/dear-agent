from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from herald.dispatch import kubernetes_launcher
from herald.kube import KubernetesClient, KubernetesError


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"data": {"job.yaml": "kind: Job\n"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received = json.loads(self.rfile.read(length))  # type: ignore[attr-defined]
        body = json.dumps({"metadata": {"name": "herald-task-x"}}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def api() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def client(api: Any) -> KubernetesClient:
    return KubernetesClient(
        namespace="herald",
        token="sa-token",
        host=f"http://127.0.0.1:{api.server_address[1]}",
    )


def test_get_configmap_returns_data(api: Any) -> None:
    assert client(api).get_configmap("herald-runner-template") == {"job.yaml": "kind: Job\n"}


def test_create_job_posts_the_manifest(api: Any) -> None:
    result = client(api).create_job({"kind": "Job"})

    assert result["metadata"]["name"] == "herald-task-x"
    assert api.received == {"kind": "Job"}


def test_from_cluster_requires_a_token(tmp_path: Any) -> None:
    with pytest.raises(KubernetesError):
        KubernetesClient.from_cluster("herald", sa_dir=tmp_path)


def test_from_cluster_prefers_the_pod_namespace(tmp_path: Any) -> None:
    (tmp_path / "token").write_text("abc\n")
    (tmp_path / "namespace").write_text("herald-validate\n")

    built = KubernetesClient.from_cluster("herald", sa_dir=tmp_path)

    assert built.token == "abc"
    assert built.namespace == "herald-validate"


def test_from_cluster_uses_the_argument_when_there_is_no_namespace_file(tmp_path: Any) -> None:
    (tmp_path / "token").write_text("abc\n")

    built = KubernetesClient.from_cluster("herald", sa_dir=tmp_path)

    assert built.namespace == "herald"


def test_kubernetes_launcher_delegates_to_create_job() -> None:
    created: list[dict] = []

    class Fake:
        def create_job(self, job: dict) -> dict:
            created.append(job)
            return job

    kubernetes_launcher(Fake())({"kind": "Job"})

    assert created == [{"kind": "Job"}]


def test_create_job_treats_already_exists_as_success() -> None:
    calls: list[str] = []

    class Once409(KubernetesClient):
        def __init__(self) -> None:
            super().__init__(namespace="herald", token="t", host="http://127.0.0.1:1")
            self.first = True

        def _request(self, method: str, path: str, **_: Any) -> dict:
            calls.append(method)
            if method == "POST" and self.first:
                self.first = False
                raise KubernetesError("POST /jobs failed: 409 AlreadyExists")
            return {"metadata": {"name": "herald-task-x"}}

    result = Once409().create_job({"metadata": {"name": "herald-task-x"}})

    assert result["metadata"]["name"] == "herald-task-x"
    assert calls == ["POST", "GET"]
