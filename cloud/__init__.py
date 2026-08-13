"""Vetromar graph host — keypair principals, invites + the replication log.

This package may import from `vetromar` (the wire model), but `vetromar`
imports `cloud` ONLY via `vetromar/hosting/` (lazily, for embedded Host
mode): the desktop sidecar bundle follows imports from `vetromar`, and this
package's heavy deps (SQLAlchemy et al.) must not load on ordinary paths.
"""
