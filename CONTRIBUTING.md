# Contributing to XEAC 🌼

Thanks for improving XEAC. This is the short version; the full guides live in the [documentation guides](documentation/).

## Getting set up

XEAC targets **macOS on Apple Silicon**. **Nix** (a flake devshell) manages the toolchain, so you get the exact pinned versions of bun, Rust, and the Tauri CLI.

```sh
git clone https://github.com/ghovax/daisy.git
cd xeac
direnv allow          # or: nix develop
```

Then follow [Development guide](documentation/development.md) to run the harness, the web UI, and the desktop app.

## Ground rules

- **Never commit secrets.** API keys go in `~/.config/xeac/configuration.yaml` or environment variables, never in a tracked file. See [Security notes](SECURITY.md).
- **Match the surrounding code.** Follow the existing naming, comment density, and structure; don't introduce a new style.
- **Keep changes focused.** One logical change per pull request, with a clear description of what and why.
- Run the checks that apply to your change (`bun run lint` in `web/`, `uv run ruff check` for the harness) before opening a PR.

## Reporting bugs and proposing features

Open a [GitHub issue](https://github.com/ghovax/daisy/issues) with enough detail to reproduce or understand the request. For security issues, follow [Security notes](SECURITY.md) instead of filing a public one.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
