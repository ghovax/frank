{
  description = "Harness web UI + LangMesh desktop app (Tauri)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = import nixpkgs { inherit system; };
    in {
      devShells.${system}.default = pkgs.mkShell {
        # Frontend + Tauri desktop toolchain, pinned by flake.lock and isolated to
        # this directory. `bun` runs the Next.js build/dev; `rustc`/`cargo` build the
        # Tauri Rust shell; `cargo-tauri` is the `tauri` CLI (dev/build/icon).
        packages = with pkgs; [
          nodejs_22
          bun
          rustc
          cargo
          cargo-tauri
          pkg-config
          # Linker dependency for Rust builds on macOS.
          libiconv
        ];

        shellHook = ''
          echo "dev env loaded: node $(node --version), bun $(bun --version), cargo $(cargo --version)"
        '';
      };
    };
}
