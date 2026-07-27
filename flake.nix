{
  description = "Project dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = import nixpkgs { inherit system; };
    in {
      devShells.${system}.default = pkgs.mkShell {
        # The full toolchain to develop and build Frank, pinned by flake.lock and
        # isolated to this directory:
        #   - uv           the Python harness's environment, and the PyInstaller freeze
        #   - bun          the web UI (Next.js) package manager and bundler
        #   - rustc/cargo  the Tauri desktop shell (Rust)
        #   - cargo-tauri  the `cargo tauri dev|build` subcommand
        #   - pkg-config   native dependency discovery during the Rust build
        #
        # `uv` belongs here because both build steps need it and neither is optional: `uv sync`
        # creates the harness's .venv, and packaging/build-daemon.sh runs the freeze through
        # `uv run pyinstaller`. It was missing, so a clean checkout entering this shell still
        # could not build the harness — the one thing the shell exists to make possible. uv
        # fetches its own Python against .python-version, so no interpreter is pinned here.
        packages = with pkgs; [
          uv
          # In the devshell as well as in the dev dependency group. It was in neither, so the
          # verification battery's lint stage silently reported it missing on every machine
          # that had not installed it by hand.
          ruff
          bun
          rustc
          cargo
          cargo-tauri
          pkg-config
        ];

        shellHook = ''
          echo "dev env loaded: uv $(uv --version | cut -d' ' -f2), bun $(bun --version), $(rustc --version)"
        '';
      };
    };
}
