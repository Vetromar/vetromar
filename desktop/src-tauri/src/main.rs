// Prevent an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::path::BaseDirectory;
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, Wry};
use tauri_plugin_autostart::MacosLauncher;
use tauri_plugin_autostart::ManagerExt as _;
use tauri_plugin_notification::NotificationExt as _;

/// Holds the Python engine subprocess so we can kill it when the app exits.
struct Sidecar(Mutex<Option<Child>>);

/// The port the sidecar reported at boot; the tray poller and menu actions
/// talk to the engine over it.
struct EnginePort(u16);

/// The tray icon plus the menu items whose enabled state tracks the engine's
/// meeting state, and the job id of a live meeting recording (for Stop).
struct TrayHandles {
    tray: tauri::tray::TrayIcon<Wry>,
    start_item: MenuItem<Wry>,
    stop_item: MenuItem<Wry>,
    job_id: Mutex<Option<String>>,
}

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

/// Menu-bar template icons drawn as raw RGBA so no asset pipeline is needed.
/// Black + alpha only; macOS recolors template images for light/dark menu bars.
/// idle = ring, detected = ring with a center dot, recording = filled disc.
fn tray_image(state: &str) -> Image<'static> {
    const S: i32 = 44;
    let (cx, cy) = (S as f32 / 2.0, S as f32 / 2.0);
    let ring_r = 13.0;
    let mut rgba = vec![0u8; (S * S * 4) as usize];
    for y in 0..S {
        for x in 0..S {
            let d = ((x as f32 + 0.5 - cx).powi(2) + (y as f32 + 0.5 - cy).powi(2)).sqrt();
            let coverage: f32 = match state {
                "recording" => (ring_r + 1.5 - d).clamp(0.0, 1.0),
                "detected" => {
                    let ring = (2.4 - (d - ring_r).abs()).clamp(0.0, 1.0);
                    let dot = (6.0 - d).clamp(0.0, 1.0);
                    ring.max(dot)
                }
                _ => (2.4 - (d - ring_r).abs()).clamp(0.0, 1.0),
            };
            let a = (coverage * 255.0) as u8;
            let i = ((y * S + x) * 4) as usize;
            rgba[i..i + 4].copy_from_slice(&[0, 0, 0, a]);
        }
    }
    Image::new_owned(rgba, S as u32, S as u32)
}

/// Bring the app forward: restore the Dock presence and show the window.
fn show_main_window(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Hide the window without quitting; the engine keeps running in the tray.
fn hide_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
}

fn engine_post(port: u16, path: &str, body: serde_json::Value) {
    let url = format!("http://127.0.0.1:{port}{path}");
    std::thread::spawn(move || {
        let _ = ureq::post(&url)
            .timeout(Duration::from_secs(10))
            .send_json(body);
    });
}

/// Poll the engine's meeting status and mirror it into the tray: icon state,
/// menu-item enablement, and a notification on the idle→detected transition.
fn spawn_status_poller(app: AppHandle) {
    std::thread::spawn(move || {
        let port = app.state::<EnginePort>().0;
        let url = format!("http://127.0.0.1:{port}/api/meetings/status");
        let mut last_state = String::from("idle");
        loop {
            std::thread::sleep(Duration::from_millis(2500));
            let status: serde_json::Value = match ureq::get(&url)
                .timeout(Duration::from_secs(5))
                .call()
                .ok()
                .and_then(|r| r.into_json().ok())
            {
                Some(v) => v,
                None => continue, // engine restarting or pre-update sidecar
            };
            let state = status["state"].as_str().unwrap_or("idle").to_string();
            let handles = app.state::<TrayHandles>();
            *handles.job_id.lock().unwrap() = status["job_id"].as_str().map(str::to_string);
            if state != last_state {
                let _ = handles.tray.set_icon(Some(tray_image(&state)));
                let _ = handles.tray.set_icon_as_template(true);
                let _ = handles.start_item.set_enabled(state == "detected");
                let _ = handles.stop_item.set_enabled(state == "recording");
                if state == "detected" {
                    let name = status["candidate"]["name"].as_str().unwrap_or("A meeting app");
                    let _ = app
                        .notification()
                        .builder()
                        .title("Meeting detected")
                        .body(format!(
                            "{name} is using the microphone. Start recording from the Vetromar menu bar icon."
                        ))
                        .show();
                }
                last_state = state;
            }
        }
    });
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, "open", "Open Vetromar", true, None::<&str>)?;
    let start_item =
        MenuItem::with_id(app, "start-meeting", "Start meeting recording", false, None::<&str>)?;
    let stop_item = MenuItem::with_id(app, "stop-recording", "Stop recording", false, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Quit Vetromar", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open_item,
            &PredefinedMenuItem::separator(app)?,
            &start_item,
            &stop_item,
            &PredefinedMenuItem::separator(app)?,
            &quit_item,
        ],
    )?;

    let tray = TrayIconBuilder::with_id("vetromar-tray")
        .icon(tray_image("idle"))
        .icon_as_template(true)
        .tooltip("Vetromar")
        .menu(&menu)
        .on_menu_event(|app, event| {
            let port = app.state::<EnginePort>().0;
            match event.id.as_ref() {
                "open" => show_main_window(app),
                "start-meeting" => {
                    engine_post(port, "/api/meetings/record", serde_json::json!({}));
                    show_main_window(app);
                }
                "stop-recording" => {
                    let job_id = app.state::<TrayHandles>().job_id.lock().unwrap().clone();
                    if let Some(job_id) = job_id {
                        engine_post(port, "/api/record/stop", serde_json::json!({ "job_id": job_id }));
                    }
                }
                "quit" => app.exit(0),
                _ => {}
            }
        })
        .build(app)?;

    app.manage(TrayHandles {
        tray,
        start_item,
        stop_item,
        job_id: Mutex::new(None),
    });
    Ok(())
}

/// Enable launch-at-login exactly once, the first time this build runs. The
/// marker file (not the autostart state) records "we already defaulted it",
/// so a user who later disables it in Settings stays disabled.
fn default_autostart_on_first_run(app: &tauri::App) {
    let Ok(home) = app.path().home_dir() else { return };
    let marker = home.join(".vetromar").join("autostart-initialized");
    if marker.exists() {
        return;
    }
    let _ = app.autolaunch().enable();
    if std::fs::create_dir_all(marker.parent().unwrap()).is_ok() {
        let _ = std::fs::write(&marker, "1\n");
    }
}

fn main() {
    // The autostart launch agent starts us with --hidden: begin in the menu
    // bar only, no window and no Dock icon, until the user opens the app.
    let start_hidden = std::env::args().any(|a| a == "--hidden");

    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        .setup(move |app| {
            let (child, port) = start_engine(app);
            app.manage(Sidecar(Mutex::new(Some(child))));
            app.manage(EnginePort(port));

            // Inject the engine's address before any page script runs, so the
            // frontend's api.js can find the local API.
            let init = format!(
                "window.__VETROMAR_API__ = \"http://127.0.0.1:{port}\";"
            );
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Vetromar")
                .inner_size(920.0, 760.0)
                .min_inner_size(560.0, 480.0)
                .visible(!start_hidden)
                .initialization_script(&init)
                .build()?;

            // Closing the window hides it; the engine and tray stay alive.
            let handle = app.handle().clone();
            window.on_window_event(move |event| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    hide_main_window(&handle);
                }
            });

            build_tray(app)?;
            default_autostart_on_first_run(app);
            spawn_status_poller(app.handle().clone());

            if start_hidden {
                #[cfg(target_os = "macos")]
                let _ = app
                    .handle()
                    .set_activation_policy(tauri::ActivationPolicy::Accessory);
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Vetromar app")
        .run(|app, event| match event {
            tauri::RunEvent::ExitRequested { .. } => {
                if let Some(state) = app.try_state::<Sidecar>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
            // Dock icon / app reactivation while the window is hidden.
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Reopen { .. } => show_main_window(app),
            _ => {}
        });
}
