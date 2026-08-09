"""Vetromar Cloud — workspace accounts + the replication log.

Server-side only. This package may import from `vetromar` (the wire model),
but `vetromar` must NEVER import `cloud`: the desktop sidecar bundle follows
imports from `vetromar`, and this package (SQLAlchemy, argon2) must stay out.
"""
