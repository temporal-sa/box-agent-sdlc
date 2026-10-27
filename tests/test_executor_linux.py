"""Opt-in actual Linux containment checks against the dedicated executor container.

Run with EXECUTOR_TEST_URL and EXECUTOR_TEST_TOKEN after starting the executor image.
The unit suite verifies fail-closed behavior when no Linux sandbox is available.
"""

import base64
import io
import json
import os
import tarfile
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("EXECUTOR_TEST_URL"), reason="Actual Linux executor not requested"
)


def archive(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as out:
        for name, payload in files.items():
            body = payload.encode()
            item = tarfile.TarInfo(name)
            item.size = len(body)
            out.addfile(item, io.BytesIO(body))
    return base64.b64encode(data.getvalue()).decode()


@pytest.fixture
def executor_http():
    with httpx.Client(
        base_url=os.environ["EXECUTOR_TEST_URL"],
        timeout=15,
        headers={"Authorization": "Bearer " + os.environ["EXECUTOR_TEST_TOKEN"]},
    ) as client:
        yield client


def execute(client, argv, files=None, timeout=5):
    return client.post(
        "/execute",
        json={
            "operation_id": "linux-test-" + uuid.uuid4().hex,
            "snapshot_b64": archive(files or {"fixture.txt": "fixture"}),
            "argv": argv,
            "timeout_seconds": timeout,
        },
    )


def test_real_executor_health_requires_containment(executor_http):
    assert executor_http.get("/readyz").status_code == 200
    assert executor_http.get("/healthz").status_code == 200


def test_real_node_suite_runs_in_snapshot(executor_http):
    result = execute(
        executor_http,
        ["node", "--test"],
        {
            "src/cart.js": "exports.discount = cents => Math.round(cents * 0.9);",
            "tests/cart.test.js": "const {test}=require('node:test');const assert=require('node:assert/strict');const {discount}=require('../src/cart.js');test('discount uses integer rounding',()=>assert.equal(discount(105),95));",
        },
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["exit_code"] == 0, body["output"]
    assert "pass 1" in body["output"]


def test_real_sandbox_hides_secrets_proc_host_and_network(executor_http):
    script = """
import json,os,pathlib,socket
blocked=False
try:
 socket.create_connection(('169.254.169.254',80),timeout=1)
except OSError:
 blocked=True
readonly=False
try:
 pathlib.Path('/usr/escape-attempt').write_text('x')
except OSError:
 readonly=True
pathlib.Path('/workspace/result.txt').write_text('allowed')
print(json.dumps({'cwd':os.getcwd(),'proc':os.listdir('/proc'),'app_visible':os.path.exists('/app'),
 'secrets':[key for key in os.environ if 'TOKEN' in key or 'API_KEY' in key],
 'network_blocked':blocked,'system_readonly':readonly,'proc_environ':os.path.exists('/proc/1/environ')}))
"""
    result = execute(executor_http, ["python3", "-c", script])
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["exit_code"] == 0, body["output"]
    state = json.loads(body["output"])
    assert state == {
        "cwd": "/workspace",
        "proc": [],
        "app_visible": False,
        "secrets": [],
        "network_blocked": True,
        "system_readonly": True,
        "proc_environ": False,
    }


def test_real_executor_bounds_output_and_time(executor_http):
    result = execute(executor_http, ["python3", "-c", "print('x'*100000)"])
    assert result.status_code == 200
    body = result.json()
    assert body["truncated"] and len(body["output"].encode()) <= 30000
    result = execute(executor_http, ["python3", "-c", "import sys;sys.stdout.buffer.write(b'\\xff'*100000)"])
    body = result.json()
    assert body["truncated"] and len(body["output"].encode()) <= 30000
    result = execute(executor_http, ["python3", "-c", "while True: pass"], timeout=1)
    body = result.json()
    assert body["timed_out"] and body["exit_code"] == 124 and body["duration_seconds"] < 4


def test_real_executor_rejects_traversal_and_wrong_token(executor_http):
    result = execute(executor_http, ["node", "--version"], {"../escape": "denied"})
    assert result.status_code == 400
    result = executor_http.post("/execute", headers={"Authorization": "Bearer invalid"}, content="bad")
    assert result.status_code == 401


def test_real_bundler_inlines_modules_css_json_and_escapes_close_tags(executor_http):
    result = executor_http.post(
        "/bundle",
        json={
            "operation_id": "bundle-" + uuid.uuid4().hex,
            "snapshot_b64": archive(
                {
                    "index.html": '<html><head><link rel="stylesheet" href="./styles.css"><script type="module" src="./src/app.js"></script></head><body><div id="products"></div></body></html>',
                    "styles.css": 'body{color:navy}.x::after{content:"</style>"}',
                    "src/app.js": 'fetch("./products.json").then(r=>r.json()).then(p=>document.querySelector("#products").textContent=p[0].name);',
                    "products.json": '[{"name":"</script><script>alert(1)</script>"}]',
                }
            ),
        },
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["passed"], body["output"]
    html = body["html"]
    assert '<script type="module" src=' not in html and 'rel="stylesheet"' not in html
    assert "globalThis" in html and "new Response" in html and "products.json" in html
    assert "</script><script>alert" not in html
    assert "<style>" in html and "navy" in html


@pytest.mark.parametrize(
    "source",
    [
        '<script type="module" src="https://example.com/app.js"></script>',
        '<link rel="stylesheet" href="https://example.com/style.css">',
        '<script type="module" src="../outside.js"></script>',
    ],
)
def test_real_bundler_rejects_external_and_escaping_resources(executor_http, source):
    result = executor_http.post(
        "/bundle",
        json={
            "operation_id": "bundle-reject-" + uuid.uuid4().hex,
            "snapshot_b64": archive({"index.html": "<html><head>" + source + "</head><body></body></html>"}),
        },
    )
    assert result.status_code == 200
    assert not result.json()["passed"] and result.json()["html"] is None
