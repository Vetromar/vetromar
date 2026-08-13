"""Host mode: the desktop app serving shared graphs from this machine.

THE import carve-out: this package is the only place in `vetromar` allowed
to import `cloud` (lazily, inside functions), so hosting stays optional and
non-hosting users never pay the import. See CONTRIBUTING.md rule 5.
"""
