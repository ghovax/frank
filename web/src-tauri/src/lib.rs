// The shared entry point for both desktop (main.rs) and mobile builds. The window
// itself — transparent titlebar, hidden title, native traffic lights — is declared
// in tauri.conf.json, so this stays intentionally thin.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running the Daisy desktop app");
}
