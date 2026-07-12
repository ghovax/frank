{
  description = "Project dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = import nixpkgs { inherit system; };
    in {
      devShells.${system}.default = pkgs.mkShell {
        # The full toolchain to develop and build Daisy, pinned by flake.lock and
        # isolated to this directory:
        #   - bun          the web UI (Next.js) package manager and bundler
        #   - rustc/cargo  the Tauri desktop shell (Rust)
        #   - cargo-tauri  the `cargo tauri dev|build` subcommand
        #   - pkg-config   native dependency discovery during the Rust build
        # The Python harness runs from a local .venv (see documentation/development.md);
        # its PyInstaller freeze for the packaged app is driven by packaging/build-sidecar.sh.
        packages = with pkgs; [
          bun
          rustc
          cargo
          cargo-tauri
          pkg-config
        ];

        shellHook = ''
          echo "dev env loaded: bun $(bun --version), $(rustc --version)"
        '';
      };
    };
}
