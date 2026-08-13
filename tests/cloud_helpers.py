"""Shared fixtures-in-functions for the keypair-era cloud tests: identities,
challenge proofs, owner bootstrap, graph creation, invites."""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vetromar.identity import Identity


def new_identity() -> Identity:
    return Identity(Ed25519PrivateKey.generate())


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def ws_headers(token: str, workspace_id: str) -> dict:
    return {**auth(token), "X-Workspace-Id": workspace_id}


def proof(client, identity: Identity) -> dict:
    """challenge → sign: the {nonce, signature} pair every proof route wants."""
    resp = client.post("/v1/auth/challenge", json={"public_key": identity.public_key})
    assert resp.status_code == 201, resp.text
    nonce = resp.json()["nonce"]
    return {"nonce": nonce, "signature": identity.sign(nonce)}


def login(client, identity: Identity) -> dict:
    resp = client.post(
        "/v1/auth/verify",
        json={"public_key": identity.public_key, **proof(client, identity)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_owner(engine, identity: Identity) -> str:
    from cloud.__main__ import set_owner

    return set_owner(engine, identity.public_key)


def create_graph(
    client, engine, owner: Identity, name="Crew", handle="host", display_name="The Host"
) -> dict:
    """Owner bootstrap + graph creation; returns {token, workspace_id, ...}."""
    make_owner(engine, owner)
    token = login(client, owner)["token"]
    resp = client.post(
        "/v1/workspaces",
        json={"name": name, "handle": handle, "display_name": display_name},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return {"token": token, **resp.json()}


def mint_invite(client, token: str, workspace_id: str, role="member") -> dict:
    resp = client.post(
        "/v1/invites", json={"role": role}, headers=ws_headers(token, workspace_id)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def join(
    client, invite_token: str, identity: Identity, handle: str, display_name: str = ""
):
    return client.post(
        "/v1/invites/accept",
        json={
            "token": invite_token,
            "public_key": identity.public_key,
            "handle": handle,
            "display_name": display_name or handle.title(),
            **proof(client, identity),
        },
    )
