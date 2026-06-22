{
  description = "Project dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = import nixpkgs { inherit system; };
    in {
      devShells.${system}.default = pkgs.mkShell {
        # The toolchain for THIS project. Pinned by flake.lock, isolated to
        # this directory. Edit freely per project.
        packages = with pkgs; [
          bun
        ];

        shellHook = ''
          echo "dev env loaded: bun $(bun --version)"
        '';
      };
    };
}
