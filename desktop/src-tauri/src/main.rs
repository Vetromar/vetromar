// Prevent an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::path::BaseDirectory;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// Holds the Python engine subprocess so we can kill it when the app exits.
struct Sidecar(Mutex<Option<Child>>);

/// Resolve the command that starts the Python engine (the FastAPI sidecar).
///
/// Order: `VETROMAR_SIDECAR` env (dev override) → the bundled resource binary
/// (production) → `vetromar` on PATH (a dev machine with the venv active). The
/// engine is always invoked as `<cmd> ui-server --port 0`, matching the CLI.
fn sidecar_command(app: &tauri::App) -> Command {
    if let Ok(custom) = std::env::var("VETROMAR_SIDECAR") {
        return Command::new(custom);
    }
    if let Ok(bundled) = app
        .path()
        .resolve("sidecar/vetromar-sidecar/vetromar-sidecar", BaseDirectory::Resource)
    {
        if bundled.exists() {
            return Command::new(bundled);
        }
    }
    Command::new("vetromar")
}

/// Start the sidecar and block until it prints `PORT=<n>` on stdout.
fn start_engine(app: &tauri::App) -> (Child, u16) {
    let mut cmd = sidecar_command(app);
    cmd.args(["ui-server", "--port", "0"])
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());

    let mut child = cmd.spawn().expect("failed to start the Vetromar engine");
    let stdout = child.stdout.take().expect("engine stdout unavailable");
    let mut reader = BufReader::new(stdout);

    let mut port: Option<u16> = None;
    let mut line = String::new();
    while reader.read_line(&mut line).unwrap_or(0) > 0 {
        if let Some(rest) = line.trim().strip_prefix("PORT=") {
            if let Ok(n) = rest.parse::<u16>() {
                port = Some(n);
                break;
            }
        }
        line.clear();
    }
    let port = port.expect("engine did not report a port");

    // Keep draining stdout so the pipe never fills and blocks the engine.
    std::thread::spawn(move || {
        let mut sink = String::new();
        while reader.read_line(&mut sink).unwrap_or(0) > 0 {
            sink.clear();
        }
    });

    (child, port)
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            let (child, port) = start_engine(app);
            app.manage(Sidecar(Mutex::new(Some(child))));

            // Inject the engine's address before any page script runs, so the
            // frontend's api.js can find the local API.
            let init = format!(
                "window.__VETROMAR_API__ = \"http://127.0.0.1:{port}\";"
            );
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Vetromar")
                .inner_size(920.0, 760.0)
                .min_inner_size(560.0, 480.0)
                .initialization_script(&init)
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Vetromar app")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app.try_state::<Sidecar>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
