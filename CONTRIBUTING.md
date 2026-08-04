# Contributing to Frank

Thanks for improving Frank. This is the short version; the full guides live in the [documentation guides](documentation/).

## Getting set up

Frank targets **macOS on Apple Silicon**. **Nix** (a flake devshell) manages the toolchain, so you get the exact pinned versions of bun, Rust, and the Tauri CLI.

```sh
git clone https://github.com/ghovax/frank.git
cd frank
direnv allow          # or: nix develop
```

Then follow the [Development guide](documentation/development.md) to run the daemon, the web UI, and the desktop app. The [`frank` command](documentation/cli.md) is the day-to-day surface.

## Ground rules

- **Never commit secrets.** API keys go in `~/.config/frank/configuration.yaml` or environment variables, never in a tracked file. See [Security notes](SECURITY.md).
- **Match the surrounding code.** Follow the existing naming, comment density, and structure; don't introduce a new style.
- **Keep changes focused.** One logical change per pull request, with a clear description of what and why.
- Run the checks that apply to your change before opening a PR: `uv run ruff check` for the harness, `bun run lint` and `bun run build` in `web/`. Two invariants are invisible in a diff and worth checking by hand — the daemon never imports the runtime, and `computer/` is never imported at module level, because both are what keep the prototype safe to fork.

## Reporting bugs and proposing features

Open a [GitHub issue](https://github.com/ghovax/frank/issues) with enough detail to reproduce or understand the request. For security issues, follow [Security notes](SECURITY.md) instead of filing a public one.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
