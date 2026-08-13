"""The 3-person shared-graph dogfood, on one machine.

Launches three fully isolated app instances (VETROMAR_HOME under a scratch
dir): A hosts "crew" and invites B and C; B notes; C shares a private
episode through the membrane; everyone converges; an MCP `serve` session on
B reads the shared graph. Run:

    .venv/bin/python .claude/skills/verify/shared_graphs_e2e.py

Exit 0 + "E2E: ALL CHECKS PASSED" is the pass signal. Cleans up after
itself (processes + scratch homes).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
VETROMAR = ROOT / ".venv" / "bin" / "vetromar"
HOST_PORT = 18795


def free_port_check(port: int) -> None:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            sys.exit(f"port {port} is in use — kill the listener first (lsof -ti :{port})")


def launch(home: Path) -> tuple[subprocess.Popen, str]:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        'backend = "api"\nonboarding_tour_done = true\nonboarding_checklist_dismissed = true\n'
    )
    env = {
        **os.environ,
        "VETROMAR_HOME": str(home),
        "ANTHROPIC_API_KEY": "sk-ant-test",  # no AI call happens in this flow
        "VETROMAR_HOST_PORT": str(HOST_PORT),
    }
    proc = subprocess.Popen(
        [str(VETROMAR), "ui-server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line.startswith("PORT="), f"unexpected first line: {line!r}"
    return proc, f"http://localhost:{line.removeprefix('PORT=')}"


def wait_job(base: str, job_id: str, timeout=30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = httpx.get(f"{base}/api/jobs/{job_id}").json()
        if job["status"] == "done":
            return job
        if job["status"] == "error":
            raise AssertionError(f"job failed: {job['error']}")
        time.sleep(0.1)
    raise AssertionError("job timed out")


def sync(base: str, graph_id: str) -> dict:
    return wait_job(base, httpx.post(f"{base}/api/graphs/{graph_id}/sync").json()["job_id"])


def graph_of(base: str, name: str) -> dict:
    return next(g for g in httpx.get(f"{base}/api/graphs").json() if g["name"] == name)


def main() -> None:
    free_port_check(HOST_PORT)
    scratch = Path(tempfile.mkdtemp(prefix="vetromar-e2e-"))
    procs: list[subprocess.Popen] = []
    try:
        (pa, A), (pb, B), (pc, C) = (launch(scratch / n) for n in ("a", "b", "c"))
        procs += [pa, pb, pc]
        print(f"instances: A={A} B={B} C={C}")

        # 1. A hosts "crew" and mints two invites.
        assert httpx.post(f"{A}/api/host", json={"enabled": True}).json()["running"]
        crew_a = httpx.post(f"{A}/api/host/graphs", json={"name": "crew", "handle": "leo"}).json()
        inv = [
            httpx.post(f"{A}/api/graphs/{crew_a['id']}/invites", json={}).json()
            for _ in range(2)
        ]
        print("1. A hosts crew ✓")

        # 2. B and C join with their own handles.
        for base, invite, handle in ((B, inv[0], "mo"), (C, inv[1], "zed")):
            joined = httpx.post(
                f"{base}/api/graphs/join",
                json={"invite_url": invite["url"], "handle": handle},
            ).json()
            assert joined.get("role") == "member", joined
            wait_job(base, joined["sync_job_id"])
        crew_b, crew_c = graph_of(B, "crew"), graph_of(C, "crew")
        print("2. B (@mo) and C (@zed) joined ✓")

        # 3. B drops a note straight into the shared graph.
        httpx.post(f"{B}/api/graphs/{crew_b['id']}/note", json={"text": "the hinge should be brass"})
        sync(B, crew_b["id"])
        print("3. B noted into crew ✓")

        # 4. C shares a PRIVATE episode through the membrane.
        note_c = httpx.post(
            f"{C}/api/graphs/private/note", json={"text": "the tide pools glow at night"}
        ).json()
        report = httpx.post(
            f"{C}/api/store/share",
            json={"graph": crew_c["id"], "episode_ids": [note_c["episode"]["id"]]},
        ).json()
        assert report["units_copied"] == 1, report
        sync(C, crew_c["id"])
        print("4. C shared a private episode ✓")

        # 5. Everyone converges; contributor attribution survives the wire.
        for base, gid in ((A, crew_a["id"]), (B, crew_b["id"]), (C, crew_c["id"])):
            sync(base, gid)
        sync(B, crew_b["id"])  # second pass: B needs C's changes too
        for base, gid, who in ((A, crew_a["id"], "A"), (B, crew_b["id"], "B"), (C, crew_c["id"], "C")):
            hits = httpx.get(
                f"{base}/api/store/search", params={"graph": gid, "text": "brass"}
            ).json()
            assert hits, f"{who} never saw B's note"
            assert hits[0]["unit"]["provenance"]["contributor"]["handle"] == "mo"
            hits = httpx.get(
                f"{base}/api/store/search", params={"graph": gid, "text": "tide pools"}
            ).json()
            assert hits, f"{who} never saw C's shared unit"
            assert hits[0]["unit"]["provenance"]["contributor"]["handle"] == "zed"
        print("5. all three converged, @mo/@zed attribution intact ✓")

        # 6. Zero quarantine anywhere; C's private graph untouched by strangers.
        for base in (A, B, C):
            rows = httpx.get(f"{base}/api/graphs", params={"counts": True}).json()
            assert all(not r.get("quarantine_count") for r in rows), rows
        assert len(httpx.get(f"{C}/api/store/episodes").json()) == 1
        print("6. zero quarantine, private graphs private ✓")

        # 7. An external agent reads the shared graph over MCP (stdio) on B.
        mcp_env = {**os.environ, "VETROMAR_HOME": str(scratch / "b")}
        mcp = subprocess.run(
            [str(VETROMAR), "serve"],
            env=mcp_env,
            input=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}},
            }) + "\n" + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n" + json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "search_units",
                           "arguments": {"text": "brass", "graph": crew_b["id"]}},
            }) + "\n",
            capture_output=True, text=True, timeout=60,
        )
        assert "brass" in mcp.stdout and '"isError": false' in mcp.stdout.replace(
            '"isError":false', '"isError": false'
        ), mcp.stdout[-500:]
        print("7. MCP serve reads the shared graph ✓")

        print("E2E: ALL CHECKS PASSED")
    finally:
        for proc in procs:
            proc.send_signal(signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
