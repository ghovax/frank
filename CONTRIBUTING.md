# Contributing to LangMesh

Thanks for improving LangMesh. This is the short version; the full guides live in the [documentation guides](documentation/).

## Getting set up

LangMesh targets **macOS on Apple Silicon**. **Nix** (a flake devshell) manages the toolchain, so you get the exact pinned versions of bun, Rust, and the Tauri CLI.

```console
$ git clone https://github.com/ghovax/langmesh.git
$ cd langmesh
$ direnv allow
```

`direnv allow` enters the flake's devshell on every `cd` into the directory. Without direnv, `nix develop` does the same thing for one shell.

Then follow the [Development guide](documentation/development.md) to run the daemon, the web UI, and the desktop app. The [`langmesh` command](documentation/cli.md) is the day-to-day surface.

## Ground rules

- **Never commit secrets.** API keys go in `~/.config/langmesh/configuration.yaml` or environment variables, never in a tracked file. See [Security notes](SECURITY.md).
- **Match the surrounding code.** Follow the existing naming, comment density, and structure; don't introduce a new style.
- **Keep changes focused.** One logical change per pull request, with a clear description of what and why.
- Run the checks that apply to your change before opening a PR: `uv run ruff check` for the harness, `bun run lint` and `bun run build` in `web/`. One invariant is invisible in a diff and worth checking by hand — `computer/` is never imported at module level, because the screen stack is heavy and most sessions never touch it.

## Reporting bugs and proposing features

Open a [GitHub issue](https://github.com/ghovax/langmesh/issues) with enough detail to reproduce or understand the request. For security issues, follow [Security notes](SECURITY.md) instead of filing a public one.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
