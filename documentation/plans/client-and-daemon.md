---
created: 2026-07-26T16:05:00Z
updated: 2026-07-26T16:05:00Z
commit: 8f3cb91
---

# The App Should Not Own the Daemon

The desktop app currently carries the harness inside itself. `packaging/build-sidecar.sh` freezes the whole Python tree into `Daisy Computer Use.app`, Tauri bundles that as a resource, and the Rust shell `posix_spawn`s it on demand, tracks its pid in a stamp file, reaps an orphan left by a previous force-quit, and signals its process group when the app quits. The app is not a client of the harness; it is the harness's parent process, with a copy of it in its own bundle.

That is one architecture, and it is not the intended one. A session is meant to be a process you can address, and the daemon is meant to be the thing that owns sessions — which makes it a peer of the command line, not a passenger of a window. The command line already treats it that way: `daisy daemon start`, `daisy daemon stop`, and everything else reaches it over a unix socket it does not own. The app should reach it the same way and be powerless when it is not there, exactly as it is powerless when a remote host is unreachable.

The pure-client behaviour already exists in the codebase, which is the strongest argument that this is a deletion rather than a design. `connection.ts` reads `if (!isTauri()) return LOCAL_DEFAULT_URL`, with a comment saying *in a plain browser it just points at the conventional local address (the user runs the daemon themselves)*. The browser path is the correct path. The desktop path is the exception, and the exception is what carries the bundling, the supervision, and about a quarter of the Rust file.

## What local becomes

Nothing, structurally. A remote target is a URL and a token, health-checked before use and reported as unreachable when it does not answer. A local target is a URL and a token too — the URL from the port file the daemon publishes, the token from the token file beside it, both in the runtime directory, both already read by `daemon_endpoint`. The only thing that made local different was that the app could conjure one into existence, and that is the capability being removed.

So the two collapse onto one code path and keep two labels. "Local" stays in the interface because it means something to a person; underneath, connecting to it is the same act as connecting to a host over a tunnel, minus the tunnel. What replaces the *start the local server* button is an empty state that says what to install and what to run, because that is now genuinely the answer.

## The part that is not deletion

Accessibility is why the frozen helper is an `.app` and not a bare binary, and that reasoning survives the decoupling intact. macOS attributes a permission to the code identity of the process that exercises it. The process that calls the Accessibility API is a session worker, which is a re-exec of the daemon, which is a re-exec of the same signed image. Wrapped in a bundle carrying the same `CFBundleName` and identifier as the desktop app and signed with the same certificate, the whole fleet folds into one stable **Daisy** row in Privacy ▸ Accessibility that survives rebuilds. A bare binary would show a raw filename; a differently-signed binary would be a second subject and prompt again.

If the daemon simply stopped being packaged, that identity would become whatever the user installed — realistically their Python interpreter. System Settings would list `python3.13`, every other tool sharing that interpreter would inherit the grant, and it would break on each virtualenv recreation. That is a permanent papercut traded for a one-time saving, so the freeze does not go away: it stops being a resource inside the GUI and becomes the daemon's own installable artifact, signed the same way, installed to its own location, with `daisy` on `PATH` pointing at the executable inside it. The command line, the daemon, and every worker are then the same signed binary entered differently — which is what the packaging comment has claimed all along, and is true whether or not a window is involved.

The two entitlements move with it. Both exist for the bundled server and say so: `com.apple.security.automation.apple-events` covers probes TCC attributes to the app as responsible parent, and `com.apple.security.cs.disable-library-validation` lets PyInstaller's dylibs load under the hardened runtime. Neither is the GUI's once the GUI launches nothing. The shell signs plain.

## Restoring what the coupling was paying for

Two conveniences came from the app owning the daemon, and both come back pointed the other way.

The first is starting everything in one action. That becomes `daisy open`, which brings the daemon up if it is not up and then launches the app by bundle identifier — `open -b com.ghovax.daisy`, not by name, so it survives the application being renamed or moved. The dependency now runs from the command line to the window rather than from the window to the harness, which is the direction that costs nothing: launching an application is not owning it, there is no bundling and no shared lifetime, and if the app is not installed the command says so and exits. `--no-daemon` covers wanting only the window.

The second is the Accessibility grant flow. macOS caches the trust check per process, so a daemon that was running before the grant will not see it; today the app kills its own child and relaunches, which happens to restart the daemon as a side effect. With the daemon outside, restarting the app no longer restarts anything, so the daemon needs to be able to restart itself on request. `daisy daemon` has `status`, `start`, `stop` and `endpoint` but no `restart`; adding it, and exposing it on the control plane, keeps that flow one click from the settings dialog. It is worth being explicit that this ends live sessions — daemon shutdown reaps every one, because the workers are its children — so the button asks first rather than discovering it afterwards.

## Costs taken deliberately

There are two installables where there was one, and a person setting up from scratch drags the app to Applications *and* installs the daemon. A single package that installs both is the obvious later smoothing and is deliberately not in this change, because bundling them back together at the installer layer is easy once they are genuinely separate and impossible to unpick if they never were.

An unsigned daemon still runs, and computer control still works, but its Accessibility grant breaks on every rebuild. That is a documentation problem rather than a code one, and `development.md` already carries the stable-identity setup it needs.

## Order

Severing comes first and is almost entirely deletion: the supervision code, the spawn command pair, the sidecar from the bundle, the symlink-restore hack that existed only because Tauri dereferences a bundled `.app`'s symlinks. Then the freeze is repurposed into the daemon's own artifact, which is the only part that cannot be verified anywhere but macOS — signing, TCC, and `open -b` are all platform-specific. Then the conveniences return.

Two naming jobs fold in where they touch the same files rather than getting commits of their own: the seventy-three occurrences of *server* meaning the daemon, about a quarter of which the first step deletes outright, and the two freshness-guard bugs in the build script — it watches `src/daisy` but not the `.agents` tree it also bundles, and it counts `__pycache__` as a source change, so merely running the harness forces a needless re-freeze.
