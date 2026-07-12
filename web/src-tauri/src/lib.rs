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

use std::collections::{BTreeSet, HashMap};
use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::webview::WebviewBuilder;
use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Runtime, WebviewUrl};
use tauri_plugin_sql::{Migration, MigrationKind};

const LOCAL_HOST: &str = "127.0.0.1";
const LOCAL_PORT: u16 = 8822;
const TRAY_ID: &str = "daisy-tray";
const MAIN_WINDOW: &str = "main";
// The embedded native webview used to preview external websites at full browser
// fidelity (real engine, top-level navigation — X-Frame-Options never applies). It
// floats over the app's preview panel, positioned to that panel's rect by the UI.
const PREVIEW_WEBVIEW: &str = "daisy-preview";
// Where the preview webview parks when hidden — far off-screen so it stays alive
// (scripts, media, session) without being visible. Cheaper and less flickery than
// tearing it down and rebuilding on every open/close.
const PREVIEW_OFFSCREEN: f64 = -32000.0;

// Supervise the bundled harness server for local mode: spawn it on request, reap
// it when the app quits, and track it via a pid stamp file for crash recovery.

// Holds the pid of the spawned local-server process (if this app started one) so it can
// be killed when the app quits. `None` means either nothing is running or the server on
// the port was started by someone else — in which case we never touch it. We track a pid
// rather than a `Child` because the server is launched via `posix_spawn` (see
// `spawn_local_server`) so it can be disclaimed.
struct LocalServer(Mutex<Option<u32>>);

// Launch the bundled server via posix_spawn, returning its pid. The child inherits the
// app's environment, working directory, and stdio, exactly as the previous
// `Command::spawn` did.
//
// The server — not the app bundle — is the process that calls the macOS Accessibility API
// for the computer-use tool, and Accessibility checks the *calling* process's own code
// identity. Rather than disclaim the server into a separate permission subject (which made
// it appear as its own "daisy-server" entry), the server is signed with the *same* code
// identity as the app (see packaging/sign-app.sh), so it satisfies the same designated
// requirement — granting "Daisy" once covers the server too, as a single clean entry.
fn spawn_local_server(executable: &std::path::Path) -> std::io::Result<u32> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    extern "C" {
        static environ: *const *const libc::c_char;
    }
    let path = CString::new(executable.as_os_str().as_bytes()).map_err(|_| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "server path contains a NUL byte")
    })?;
    unsafe {
        let mut attributes: libc::posix_spawnattr_t = std::mem::zeroed();
        if libc::posix_spawnattr_init(&mut attributes) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        let argv = [path.as_ptr(), std::ptr::null()];
        let mut pid: libc::pid_t = 0;
        let code = libc::posix_spawn(
            &mut pid,
            path.as_ptr(),
            std::ptr::null(),
            &attributes,
            argv.as_ptr() as *const *mut libc::c_char,
            environ as *const *mut libc::c_char,
        );
        libc::posix_spawnattr_destroy(&mut attributes);
        if code != 0 {
            return Err(std::io::Error::from_raw_os_error(code));
        }
        Ok(pid as u32)
    }
}

// Whether a pid we spawned is still alive (signal 0 probes without delivering anything).
fn process_alive(pid: u32) -> bool {
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

struct SshTunnels(Mutex<HashMap<String, SshTunnel>>);

struct SshTunnel {
    url: String,
    child: Child,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct SshHost {
    alias: String,
    host_name: String,
    user: String,
    port: u16,
    identity_files: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SshTunnelRequest {
    profile_id: String,
    host_alias: String,
    host_name: Option<String>,
    user: Option<String>,
    port: Option<u16>,
    identity_file: Option<String>,
    local_port: Option<u16>,
    remote_port: Option<u16>,
}

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
        .join("Daisy Computer Use.app")
        .join("Contents")
        .join("MacOS")
        .join("daisy"))
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
        if let Some(pid) = guard.take() {
            unsafe {
                libc::kill(pid as libc::pid_t, libc::SIGTERM);
                // Reap the child so it does not linger as a zombie (it is still our
                // direct child — disclaiming changes TCC attribution, not parentage).
                let mut status = 0;
                libc::waitpid(pid as libc::pid_t, &mut status, 0);
            }
        }
    }
    let _ = std::fs::remove_file(pid_stamp_path());
}

fn home_ssh_config_path() -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or_else(|| "HOME is not set.".to_string())?;
    Ok(PathBuf::from(home).join(".ssh").join("config"))
}

fn configured_ssh_aliases() -> Result<Vec<String>, String> {
    let path = home_ssh_config_path()?;
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = std::fs::File::open(&path)
        .map_err(|error| format!("could not read {}: {error}", path.display()))?;
    let reader = BufReader::new(file);
    let mut aliases = BTreeSet::new();
    for line in reader.lines() {
        let line = line.map_err(|error| format!("could not read {}: {error}", path.display()))?;
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let mut parts = trimmed.split_whitespace();
        let Some(keyword) = parts.next() else {
            continue;
        };
        if !keyword.eq_ignore_ascii_case("host") {
            continue;
        }
        for alias in parts {
            if !alias.contains('*') && !alias.contains('?') && alias != "!" {
                aliases.insert(alias.to_string());
            }
        }
    }
    Ok(aliases.into_iter().collect())
}

fn resolve_ssh_host(alias: &str) -> SshHost {
    let mut host = SshHost {
        alias: alias.to_string(),
        host_name: alias.to_string(),
        user: String::new(),
        port: 22,
        identity_files: Vec::new(),
    };
    let output = Command::new("ssh").arg("-G").arg(alias).output();
    let Ok(output) = output else {
        return host;
    };
    if !output.status.success() {
        return host;
    }
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let Some((key, value)) = line.split_once(' ') else {
            continue;
        };
        match key {
            "hostname" => host.host_name = value.to_string(),
            "user" => host.user = value.to_string(),
            "port" => {
                host.port = value.parse().unwrap_or(22);
            }
            "identityfile" => host.identity_files.push(value.to_string()),
            _ => {}
        }
    }
    host
}

#[tauri::command]
fn list_ssh_hosts() -> Result<Vec<SshHost>, String> {
    Ok(configured_ssh_aliases()?
        .iter()
        .map(|alias| resolve_ssh_host(alias))
        .collect())
}

fn open_local_port(requested: Option<u16>) -> Result<u16, String> {
    if let Some(port) = requested.filter(|port| *port > 0) {
        return Ok(port);
    }
    let listener = TcpListener::bind((LOCAL_HOST, 0))
        .map_err(|error| format!("could not allocate a local tunnel port: {error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| format!("could not inspect local tunnel port: {error}"))?
        .port();
    drop(listener);
    Ok(port)
}

#[tauri::command]
fn start_ssh_tunnel(
    state: tauri::State<'_, SshTunnels>,
    request: SshTunnelRequest,
) -> Result<String, String> {
    let mut guard = state.0.lock().map_err(|error| error.to_string())?;
    if let Some(existing) = guard.get_mut(&request.profile_id) {
        if matches!(existing.child.try_wait(), Ok(None)) {
            return Ok(existing.url.clone());
        }
    }
    if let Some(mut stale) = guard.remove(&request.profile_id) {
        let _ = stale.child.kill();
        let _ = stale.child.wait();
    }

    let local_port = open_local_port(request.local_port)?;
    let remote_port = request.remote_port.unwrap_or(8822);
    let url = format!("http://{LOCAL_HOST}:{local_port}");
    let mut command = Command::new("ssh");
    command
        .arg("-N")
        .arg("-o")
        .arg("ExitOnForwardFailure=yes")
        .arg("-L")
        .arg(format!("{LOCAL_HOST}:{local_port}:127.0.0.1:{remote_port}"));
    if let Some(host_name) = request.host_name.as_deref().filter(|value| !value.trim().is_empty()) {
        command.arg("-o").arg(format!("HostName={}", host_name.trim()));
    }
    if let Some(user) = request.user.as_deref().filter(|value| !value.trim().is_empty()) {
        command.arg("-l").arg(user.trim());
    }
    if let Some(port) = request.port {
        command.arg("-p").arg(port.to_string());
    }
    if let Some(identity_file) = request.identity_file.as_deref().filter(|value| !value.trim().is_empty()) {
        command.arg("-i").arg(identity_file.trim());
    }
    command.arg(&request.host_alias);
    let child = command
        .spawn()
        .map_err(|error| format!("failed to start SSH tunnel: {error}"))?;
    guard.insert(request.profile_id, SshTunnel { url: url.clone(), child });
    Ok(url)
}

fn kill_ssh_tunnels(state: &SshTunnels) {
    if let Ok(mut guard) = state.0.lock() {
        for (_, mut tunnel) in guard.drain() {
            let _ = tunnel.child.kill();
            let _ = tunnel.child.wait();
        }
    }
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
    if let Some(pid) = guard.as_ref() {
        if process_alive(*pid) {
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

    let pid = spawn_local_server(&executable)
        .map_err(|error| format!("failed to start the local server: {error}"))?;
    let _ = std::fs::write(pid_stamp_path(), pid.to_string());
    *guard = Some(pid);
    Ok(local_base_url())
}

#[tauri::command]
fn stop_local_server(state: tauri::State<'_, LocalServer>) -> Result<(), String> {
    kill_local_server(&state);
    Ok(())
}

// Quit and relaunch the app. Used by the "grant Accessibility" flow: macOS only reflects a
// new Accessibility grant to the bundled server on a fresh launch, so after the user grants
// it we offer a one-click restart. Reap the current server *first* so the relaunched instance
// can bind its port cleanly (a surviving old server holds :8822 and the new window would have
// no backend to reach); then relaunch, which spawns a fresh server that sees the grant.
#[tauri::command]
fn restart_app(app: AppHandle, state: tauri::State<'_, LocalServer>) {
    kill_local_server(&state);
    app.restart();
}

// The embedded native webview used to preview external websites at full browser
// fidelity, created on first use and positioned by the UI over the preview panel.

// Only ever preview real web pages; anything else is rejected so the embedded
// webview can never be pointed at a local or non-web scheme.
fn parse_preview_url(url: &str) -> Result<tauri::Url, String> {
    let parsed = tauri::Url::parse(url).map_err(|error| format!("invalid preview url: {error}"))?;
    match parsed.scheme() {
        "http" | "https" => Ok(parsed),
        other => Err(format!("unsupported preview url scheme: {other}")),
    }
}

// Show the preview webview at the given rect (logical/CSS pixels relative to the
// main window's content), creating it on first use and navigating it when the URL
// changes. The UI drives every call from the preview panel's measured bounds.
#[tauri::command]
fn artifact_show(
    app: AppHandle,
    url: String,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    let target = parse_preview_url(&url)?;
    let width = width.max(1.0);
    let height = height.max(1.0);
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        // Re-navigate only when the destination actually changed, so repositioning
        // the panel (scroll/resize) never reloads the page.
        if webview.url().map(|current| current != target).unwrap_or(true) {
            webview.navigate(target).map_err(|error| error.to_string())?;
        }
        webview
            .set_position(LogicalPosition::new(x, y))
            .map_err(|error| error.to_string())?;
        webview
            .set_size(LogicalSize::new(width, height))
            .map_err(|error| error.to_string())?;
        return Ok(());
    }
    // A WebviewWindow is a Window plus its primary Webview; the multiwebview
    // `add_child` lives on the underlying Window, reached via `.as_ref().window()`.
    let main_window = app
        .get_webview_window(MAIN_WINDOW)
        .ok_or_else(|| "main window is not available".to_string())?;
    let window = main_window.as_ref().window();
    let builder = WebviewBuilder::new(PREVIEW_WEBVIEW, WebviewUrl::External(target));
    window
        .add_child(
            builder,
            LogicalPosition::new(x, y),
            LogicalSize::new(width, height),
        )
        .map_err(|error| error.to_string())?;
    Ok(())
}

// Move/resize the preview webview to a new rect without touching its navigation —
// called as the panel is resized or the window layout shifts.
#[tauri::command]
fn artifact_set_bounds(
    app: AppHandle,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        webview
            .set_position(LogicalPosition::new(x, y))
            .map_err(|error| error.to_string())?;
        webview
            .set_size(LogicalSize::new(width.max(1.0), height.max(1.0)))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Park the preview webview off-screen (kept alive) so the panel can close, a modal
// can cover the area, or the transcript can scroll without the native layer showing
// through.
#[tauri::command]
fn artifact_hide(app: AppHandle) -> Result<(), String> {
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        webview
            .set_position(LogicalPosition::new(PREVIEW_OFFSCREEN, PREVIEW_OFFSCREEN))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Tear the preview webview down entirely (its scripts, media, and network stop).
#[tauri::command]
fn artifact_close(app: AppHandle) -> Result<(), String> {
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        webview.close().map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Build and manage the macOS tray menu with recent conversations and lifecycle commands.

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

// Application entry point: register Tauri plugins, migrations, commands, and the tray.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let migrations = vec![
        Migration {
            version: 1,
            description: "create_connection_store",
            sql: include_str!("../migrations/001_create_connection_store.sql"),
            kind: MigrationKind::Up,
        },
        Migration {
            version: 2,
            description: "map_sessions_to_connections",
            sql: include_str!("../migrations/002_map_sessions_to_connections.sql"),
            kind: MigrationKind::Up,
        },
        Migration {
            version: 3,
            description: "add_ssh_connections",
            sql: include_str!("../migrations/003_add_ssh_connections.sql"),
            kind: MigrationKind::Up,
        },
    ];

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(
            tauri_plugin_sql::Builder::default()
                .add_migrations("sqlite:internal.db", migrations)
                .build(),
        )
        .manage(LocalServer(Mutex::new(None)))
        .manage(SshTunnels(Mutex::new(HashMap::new())))
        .invoke_handler(tauri::generate_handler![
            start_local_server,
            stop_local_server,
            restart_app,
            list_ssh_hosts,
            start_ssh_tunnel,
            update_tray_recent,
            artifact_show,
            artifact_set_bounds,
            artifact_hide,
            artifact_close
        ])
        .setup(|app| {
            let handle = app.handle();
            let menu = build_tray_menu(handle, &[])?;
            // A dedicated monochrome, transparent TEMPLATE image for the menubar — not the
            // full-colour app icon, which renders as an opaque white square. `as_template`
            // lets macOS tint it to match the menubar (dark/light), like native items.
            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray-icon.png"))
                .expect("bundled tray icon");
            TrayIconBuilder::with_id(TRAY_ID)
                .icon(tray_icon)
                .icon_as_template(true)
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
                if let Some(state) = app_handle.try_state::<SshTunnels>() {
                    kill_ssh_tunnels(&state);
                }
            }
            _ => {}
        });
}
