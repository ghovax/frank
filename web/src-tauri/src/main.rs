// Prevents an extra console window on Windows in release builds. Does nothing on macOS.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    langmesh_lib::run()
}
