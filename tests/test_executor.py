import base64
import io
import tarfile

import pytest
from fastapi.testclient import TestClient

from box_agent_sdlc import executor


def snapshot(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, value in entries:
            item = tarfile.TarInfo(name)
            if isinstance(value, bytes):
                item.size = len(value)
                archive.addfile(item, io.BytesIO(value))
            else:
                item.type, item.linkname = value
                archive.addfile(item)
    return base64.b64encode(buffer.getvalue()).decode()


def test_snapshot_extracts_only_regular_workspace_files(tmp_path):
    executor.extract_snapshot(snapshot([("src/cart.js", b"export const x=1;")]), tmp_path)
    assert (tmp_path / "src/cart.js").read_text() == "export const x=1;"


@pytest.mark.parametrize(
    "name,value",
    [
        ("../escape", b"x"),
        ("/absolute", b"x"),
        (".git/config", b"x"),
        ("src/link", (tarfile.SYMTYPE, "/etc/passwd")),
        ("src/hard", (tarfile.LNKTYPE, "src/target")),
        ("src/device", (tarfile.CHRTYPE, "")),
    ],
)
def test_snapshot_rejects_traversal_links_and_devices(tmp_path, name, value):
    with pytest.raises(ValueError):
        executor.extract_snapshot(snapshot([(name, value)]), tmp_path)


def test_snapshot_limits_expanded_content(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "MAX_SNAPSHOT_BYTES", 20_000)
    with pytest.raises(ValueError, match="size limit"):
        executor.extract_snapshot(snapshot([("big", b"x" * 25_000)]), tmp_path)


def test_executor_authenticates_before_parsing_snapshot(monkeypatch):
    monkeypatch.setenv("EXECUTOR_TOKEN", "executor-only-secret")
    client = TestClient(executor.app)
    assert client.post("/execute", content=b"invalid").status_code == 401
    assert (
        client.post("/execute", content=b"invalid", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        client.post(
            "/execute", content=b"invalid", headers={"Authorization": "Bearer executor-only-secret"}
        ).status_code
        == 422
    )


def test_executor_fails_closed_without_linux_namespace_sandbox(monkeypatch):
    monkeypatch.setenv("EXECUTOR_TOKEN", "secret")
    monkeypatch.setattr(executor.sys, "platform", "darwin")
    client = TestClient(executor.app)
    result = client.post(
        "/execute",
        headers={"Authorization": "Bearer secret"},
        json={
            "operation_id": "no-sandbox",
            "snapshot_b64": snapshot([("x", b"y")]),
            "argv": ["node", "--version"],
        },
    )
    assert result.status_code == 503
    assert "execution is disabled" in result.json()["detail"]


def test_bubblewrap_command_isolates_network_proc_secrets_and_host_files(monkeypatch, tmp_path):
    monkeypatch.setattr(executor.sys, "platform", "linux")
    monkeypatch.setattr(executor.shutil, "which", lambda name: "/usr/bin/bwrap")
    args = executor.sandbox_command(tmp_path, ["node", "--test", "tests/"])
    assert "--unshare-all" in args and "--clearenv" in args and "--cap-drop" in args
    proc = args.index("/proc")
    assert args[proc - 1] == "--tmpfs"
    assert "--proc" not in args and "/workspace" in args
    assert "--share-net" not in args
    assert not any("TOKEN" in arg or "API_KEY" in arg for arg in args)


def test_executor_returns_bounded_results_and_reuses_matching_operation(monkeypatch):
    monkeypatch.setenv("EXECUTOR_TOKEN", "secret")
    calls = []

    async def fake_run(job, root):
        calls.append(job.operation_id)
        assert (root / "x").read_text() == "y"
        return {"exit_code": 0, "output": "passed", "sandbox": "test-double"}

    monkeypatch.setattr(executor, "run_isolated", fake_run)
    client = TestClient(executor.app)
    body = {
        "operation_id": "cache-test",
        "snapshot_b64": snapshot([("x", b"y")]),
        "argv": ["node", "--version"],
    }
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/execute", json=body, headers=headers).json()["exit_code"] == 0
    assert client.post("/execute", json=body, headers=headers).status_code == 200
    assert calls == ["cache-test"]
    body["argv"] = ["node", "-v"]
    assert client.post("/execute", json=body, headers=headers).status_code == 409


def test_bundler_authenticates_and_invokes_only_trusted_helper(monkeypatch):
    monkeypatch.setenv("EXECUTOR_TOKEN", "secret")

    async def fake_run(job, root):
        assert job.argv == ["node", executor.BUNDLER_SCRIPT]
        assert (root / "index.html").read_text() == "<html></html>"
        (root / executor.BUNDLE_OUTPUT).write_text("<html><body>self contained</body></html>")
        return {"exit_code": 0, "output": "built", "timed_out": False}

    monkeypatch.setattr(executor, "run_isolated", fake_run)
    client = TestClient(executor.app)
    assert client.post("/bundle", content="bad").status_code == 401
    result = client.post(
        "/bundle",
        headers={"Authorization": "Bearer secret"},
        json={
            "operation_id": "bundle-unit",
            "snapshot_b64": snapshot([("index.html", b"<html></html>")]),
            "argv": ["/bin/sh", "-c", "this caller field is ignored"],
        },
    )
    assert result.status_code == 200 and result.json()["passed"]
    assert result.json()["html"] == "<html><body>self contained</body></html>"


def test_bundler_does_not_return_source_if_helper_failed(monkeypatch):
    monkeypatch.setenv("EXECUTOR_TOKEN", "secret")

    async def fake_run(job, root):
        return {"exit_code": 1, "output": "Unsupported external module", "timed_out": False}

    monkeypatch.setattr(executor, "run_isolated", fake_run)
    result = TestClient(executor.app).post(
        "/bundle",
        headers={"Authorization": "Bearer secret"},
        json={"operation_id": "bundle-failed", "snapshot_b64": snapshot([("index.html", b"<html></html>")])},
    )
    assert result.status_code == 200
    assert not result.json()["passed"] and result.json()["html"] is None
