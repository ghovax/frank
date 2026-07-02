// The Rust core of the Daisy desktop app.
//
// Responsibilities beyond hosting the webview:
//   1. Register the front-end-local SQLite store (connection profiles + UI prefs),
//      separate from the harness server's own history.db.
//   2. Supervise the bundled harness server for "local" mode: spawn it on request,
//      and reap it when the app truly quits (not when the window is merely closed).
//   3. Behave like a proper macOS menu-bar app: a tray menu (New Chat, Recent
//      Conversations, Open Daisy, Quit), and a close button that hides the window
//      and keeps the app (and its server) alive in the dock until the user quits.
//
// The window chrome — hidden titlebar with native macOS traffic lights overlaid on
// the content — is declared in tauri.conf.json.

use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Runtime};
use tauri_plugin_sql::{Migration, MigrationKind};

const LOCAL_HOST: &str = "127.0.0.1";
const LOCAL_PORT: u16 = 8822;
const TRAY_ID: &str = "daisy-tray";
const MAIN_WINDOW: &str = "main";

// ---- local server supervision ---------------------------------------------

// Holds the spawned local-server process (if this app started one) so it can be
// killed when the app quits. `None` means either nothing is running or the server
// on the port was started by someone else — in which case we never touch it.
struct LocalServer(Mutex<Option<Child>>);

fn local_base_url() -> String {
    format!("http://{LOCAL_HOST}:{LOCAL_PORT}")
}

fn local_port_open() -> bool {
    let address: SocketAddr = format!("{LOCAL_HOST}:{LOCAL_PORT}")
        .parse()
        .expect("valid local socket address");
    TcpStream::connect_timeout(&address, Duration::from_millis(300)).is_ok()
}

// Path to the bundled frozen harness server inside the app's resources. It is an
// optional resource (tauri.conf.json > bundle.resources): present when the
// PyInstaller build produced it, otherwise absent and reported clearly.
fn server_executable<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, String> {
    let resources = app
        .path()
        .resource_dir()
        .map_err(|error| format!("could not resolve resource dir: {error}"))?;
    Ok(resources
        .join("server-bin")
        .join("daisy-server")
        .join("daisy-server"))
}

// Stamp file recording the pid of the local server we spawned, so the next launch
// can reap one orphaned by a hard crash (paths that can't run our cleanup).
fn pid_stamp_path() -> PathBuf {
    std::env::temp_dir().join("daisy-server.pid")
}

fn reap_stale_server() {
    let path = pid_stamp_path();
    if let Ok(contents) = std::fs::read_to_string(&path) {
        if let Ok(pid) = contents.trim().parse::<i32>() {
            let _ = Command::new("/bin/kill").arg(pid.to_string()).status();
        }
    }
    let _ = std::fs::remove_file(&path);
}

fn kill_local_server(state: &LocalServer) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    let _ = std::fs::remove_file(pid_stamp_path());
}

#[tauri::command]
fn start_local_server(
    app: AppHandle,
    state: tauri::State<'_, LocalServer>,
) -> Result<String, String> {
    if local_port_open() {
        return Ok(local_base_url());
    }
    // Port is free, so any server we spawned before is gone — clean up a possible
    // orphan from a prior force-quit before starting a fresh one.
    reap_stale_server();

    let mut guard = state.0.lock().map_err(|error| error.to_string())?;
    if let Some(child) = guard.as_mut() {
        if matches!(child.try_wait(), Ok(None)) {
            return Ok(local_base_url());
        }
    }

    let executable = server_executable(&app)?;
    if !executable.exists() {
        return Err(format!(
            "The bundled local server is not available (expected at {}). Start the harness \
             yourself with `uv run python server.py`, or connect to a remote server instead.",
            executable.display()
        ));
    }

    let child = Command::new(&executable)
        .spawn()
        .map_err(|error| format!("failed to start the local server: {error}"))?;
    let _ = std::fs::write(pid_stamp_path(), child.id().to_string());
    *guard = Some(child);
    Ok(local_base_url())
}

#[tauri::command]
fn stop_local_server(state: tauri::State<'_, LocalServer>) -> Result<(), String> {
    kill_local_server(&state);
    Ok(())
}

// ---- tray menu -------------------------------------------------------------

// One recent conversation, pushed from the UI so the tray can list them.
#[derive(Debug, Deserialize)]
struct RecentItem {
    id: String,
    title: String,
}

// Build the tray menu, with the recent-conversations submenu populated from the
// UI's latest sessions (empty on first launch).
fn build_tray_menu<R: Runtime>(
    app: &AppHandle<R>,
    recents: &[RecentItem],
) -> tauri::Result<Menu<R>> {
    let new_chat = MenuItem::with_id(app, "new_chat", "New Chat", true, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open Daisy", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Daisy", true, None::<&str>)?;
    let separator_one = PredefinedMenuItem::separator(app)?;
    let separator_two = PredefinedMenuItem::separator(app)?;

    // The recent items own their menu entries; keep them alive for the build.
    let recent_entries: Vec<MenuItem<R>> = recents
        .iter()
        .map(|item| MenuItem::with_id(app, &item.id, &item.title, true, None::<&str>))
        .collect::<tauri::Result<_>>()?;

    let recent_submenu = if recent_entries.is_empty() {
        let empty = MenuItem::with_id(app, "recent_none", "No recent conversations", false, None::<&str>)?;
        Submenu::with_items(app, "Recent Conversations", true, &[&empty])?
    } else {
        let refs: Vec<&dyn tauri::menu::IsMenuItem<R>> = recent_entries
            .iter()
            .map(|entry| entry as &dyn tauri::menu::IsMenuItem<R>)
            .collect();
        Submenu::with_items(app, "Recent Conversations", true, &refs)?
    };

    Menu::with_items(
        app,
        &[
            &new_chat as &dyn tauri::menu::IsMenuItem<R>,
            &recent_submenu,
            &separator_one,
            &open,
            &separator_two,
            &quit,
        ],
    )
}

fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

// Route a tray menu click. Static ids are handled by name; anything else is a
// recent conversation's session id, forwarded to the UI to open.
fn handle_tray_menu<R: Runtime>(app: &AppHandle<R>, id: &str) {
    match id {
        "new_chat" => {
            show_main_window(app);
            let _ = app.emit("daisy://new-chat", ());
        }
        "open" => show_main_window(app),
        "quit" => app.exit(0),
        "recent_none" => {}
        session_id => {
            show_main_window(app);
            let _ = app.emit("daisy://open-session", session_id.to_string());
        }
    }
}

// Rebuild the tray menu with the UI's latest recent conversations.
#[tauri::command]
fn update_tray_recent(app: AppHandle, items: Vec<RecentItem>) -> Result<(), String> {
    let menu = build_tray_menu(&app, &items).map_err(|error| error.to_string())?;
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        tray.set_menu(Some(menu)).map_err(|error| error.to_string())?;
    }
    Ok(())
}

// ---- app entry -------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let migrations = vec![Migration {
        version: 1,
        description: "create_connection_store",
        sql: "CREATE TABLE IF NOT EXISTS connections (\
                id TEXT PRIMARY KEY, \
                name TEXT NOT NULL, \
                url TEXT NOT NULL, \
                kind TEXT NOT NULL DEFAULT 'remote', \
                created_at TEXT NOT NULL, \
                last_used_at TEXT\
              ); \
              CREATE TABLE IF NOT EXISTS app_state (\
                key TEXT PRIMARY KEY, \
                value TEXT NOT NULL\
              );",
        kind: MigrationKind::Up,
    }];

    tauri::Builder::default()
        .plugin(
            tauri_plugin_sql::Builder::default()
                .add_migrations("sqlite:daisy.db", migrations)
                .build(),
        )
        .manage(LocalServer(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            start_local_server,
            stop_local_server,
            update_tray_recent
        ])
        .setup(|app| {
            let handle = app.handle();
            let menu = build_tray_menu(handle, &[])?;
            TrayIconBuilder::with_id(TRAY_ID)
                .icon(app.default_window_icon().expect("bundled app icon").clone())
                .tooltip("Daisy")
                .menu(&menu)
                .on_menu_event(|app, event| handle_tray_menu(app, event.id().as_ref()))
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // The close button hides the window and keeps the app alive in the dock
            // and menu bar (with the local server still running). Only an explicit
            // Quit — Cmd+Q or the tray's Quit — actually terminates.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the Daisy desktop app")
        .run(|app_handle, event| match event {
            // Clicking the dock icon while hidden brings the window back.
            tauri::RunEvent::Reopen { .. } => show_main_window(app_handle),
            // A real quit reaps the local server we started. Window closes never
            // reach here (they're prevented above), so this only fires on quit.
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => {
                if let Some(state) = app_handle.try_state::<LocalServer>() {
                    kill_local_server(&state);
                }
            }
            _ => {}
        });
}
