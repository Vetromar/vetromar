"""PyInstaller entrypoint for the bundled engine.

Identical surface to the `vetromar` console script, so the Tauri shell can call
`vetromar-sidecar ui-server --port 0` exactly as it would call the dev CLI.
"""

import multiprocessing

from vetromar.cli import main

if __name__ == "__main__":
    # Frozen apps that use multiprocessing (torch DataLoader / joblib workers in
    # the capture stack) re-exec this binary for each worker; without this the
    # re-exec's bootstrap args hit the typer CLI parser ("No such option: -B").
    multiprocessing.freeze_support()
    main()
