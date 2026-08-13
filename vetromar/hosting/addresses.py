"""Best-effort detection of addresses members could reach this host at.

The advertised address is ultimately the host's CHOICE (config
`host_advertise_url`) — these are just the candidates the picker offers:
the LAN IP, and the tailnet address when Tailscale is around (the
no-port-forwarding path, and the founder's own: CGNAT apartment wifi).
Everything here fails soft to an empty list — detection must never break
the Host panel."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess

# The Mac App Store build doesn't put `tailscale` on PATH.
_TAILSCALE_BINARIES = (
    "tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def _lan_ip() -> str | None:
    """The interface a default route would use — no packets are sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET: never actually routed to
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _tailscale_status() -> dict | None:
    for binary in _TAILSCALE_BINARIES:
        path = shutil.which(binary) or (binary if binary.startswith("/") else None)
        if not path:
            continue
        try:
            out = subprocess.run(
                [path, "status", "--json"],
                capture_output=True,
                timeout=3,
                check=True,
            )
            return json.loads(out.stdout)
        except Exception:  # noqa: BLE001 — not installed/running/parsable
            continue
    return None


def candidate_addresses(port: int) -> list[dict]:
    """[{kind, address, url}] — LAN first, then tailnet IP/DNS name."""
    candidates: list[dict] = []

    lan = _lan_ip()
    if lan:
        candidates.append({"kind": "lan", "address": lan, "url": f"http://{lan}:{port}"})

    status = _tailscale_status()
    if status:
        self_info = status.get("Self") or {}
        for ip in self_info.get("TailscaleIPs") or []:
            if ":" in ip:
                continue  # keep the picker simple: v4 only
            candidates.append(
                {"kind": "tailscale", "address": ip, "url": f"http://{ip}:{port}"}
            )
        dns = (self_info.get("DNSName") or "").rstrip(".")
        if dns:
            candidates.append(
                {"kind": "tailscale-dns", "address": dns, "url": f"http://{dns}:{port}"}
            )

    return candidates
