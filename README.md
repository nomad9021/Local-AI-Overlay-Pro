# Local AI Overlay Pro

An always-on-top HUD and toast notifications that show what Ollama is doing
right now, **on this machine and on every other computer and server you watch**.
For each model it shows:

- whether it's resident in memory
- **whether it's actually generating or sitting warm, waiting for work**
- how long it has left before Ollama evicts it
- its live tok/s

GPU utilization and VRAM are shown per host. It runs on **Windows and Linux**
from the same code.

- **One card per host.** Each card has its own border colour, a name tag, a kind
  badge (`LOCAL` / `PC` / `SERVER`), and coloured tags (`gpu`, `prod`, `lab`, …).
  You can stack the cards vertically or lay them side by side, in any screen
  corner.
- **Passthrough agent.** Run `agent.py` on any box, headless servers included.
  It shares the full local picture (true ACTIVE/WAITING state, tok/s, GPU) with
  overlays elsewhere. It is read-only and token-protected.
  - If you can't install anything on a server, point the overlay at that box's
    bare Ollama API instead.
- **Settings & dashboard window** (⚙ on the HUD, or the tray menu) with these tabs:
  - **Dashboard:** live grid of every host
  - **Hosts:** add, test, or auto-discover machines on the LAN
  - **Tags:** tag colours
  - **Appearance:** state colours, borders, opacity, layout
  - **General:** polling, pinned models, notifications, start at login
  - **Sharing:** make this machine visible to other overlays
- **Toasts** for:
  - local model loads, switches, and unloads
  - a remote host going offline or coming back
  - optionally, remote model loads

## Install (one line)

**Windows** (PowerShell, no admin needed):

```powershell
irm https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.ps1 | iex
```

**Linux** (as your normal desktop user; sudo is used only for packages):

```bash
curl -fsSL https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.sh | bash
```

Each installer does the following:

1. Fetches the app.
2. Creates a private Python venv. On Windows it installs Python 3.12 with winget
   if it's missing; on Linux it installs `python3-venv` and Qt's X11 libraries
   with your package manager.
3. Adds a Start-menu / app-menu entry.
4. Enables start-at-login.
5. Opens the settings window.

### Agent only, for servers and other machines

Run this on each machine you want to monitor *from* somewhere else. It needs no
display.

```bash
# Linux — systemd service; prints the address + token to add on your overlay
curl -fsSL https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.sh | bash -s -- --agent
```

```powershell
# Windows — run elevated to start at boot and open the firewall port
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.ps1))) -Agent
```

The agent installation works like this:

- **Linux, as a normal user:** runs as a `systemd --user` service with lingering
  enabled, so it survives logout.
- **Linux, as root or with `--system`:** installs to `/opt` and runs as a
  dedicated unprivileged `laio-agent` user. The token is stored mode 0600 in
  `/etc/local-ai-overlay-pro/agent.json`.
- **Windows, elevated:** registers a boot-time task and opens TCP 11480 on
  Private/Domain networks.
- **Windows, non-elevated:** starts the agent at login.
- **Both:** a token is generated and printed at the end.

Installer options:

| Linux (`bash -s -- …`) | Windows (`-…`) | Effect |
| --- | --- | --- |
| `--agent` | `-Agent` | Install the headless agent instead of the overlay |
| `--token T` | `-Token T` | Use this token (`''` for none) instead of generating one |
| `--port N` | `-Port N` | Agent port (default 11480) |
| `--no-autostart` | `-NoAutostart` | Don't start at login/boot |
| `--system` | (run elevated) | System-wide agent |
| `--uninstall [--purge]` | `-Uninstall` | Remove (`--purge` also deletes settings) |

Settings live in `%APPDATA%\LocalAIOverlayPro` on Windows and
`~/.config/local-ai-overlay-pro` on Linux. They survive reinstalls.

### From source

```
pip install -r requirements.txt
python app.py              # overlay  (--settings opens the settings window)
python agent.py --init-config && python agent.py   # agent
```

## Monitoring other computers and servers

Open **Settings → Hosts → Add** (or **Discover…**) and pick a mode:

| Mode | Needs on that machine | You get |
| --- | --- | --- |
| **Agent** (port 11480) | `agent.py`, via the one-liner above, or **Settings → Sharing** turned on in its own overlay | Everything the local HUD shows: true ACTIVE/WAITING from the runners, live tok/s, GPU%/VRAM, Ollama version |
| **Ollama API** (port 11434) | Ollama started with `OLLAMA_HOST=0.0.0.0` | Loaded models, VRAM and eviction countdowns. ACTIVE flashes only when a request *completes* (the `expires_at` layer below). No tok/s, no GPU. |

**Discover** finds machines two ways:

- It listens for agents' LAN beacons (UDP 11481).
- It sweeps your /24 for Ollama (11434) and agents (11480).

It only runs when you click the button. Hosts are polled concurrently every few
seconds (configurable), so one slow or dead host never delays the others. An
unreachable host shows a red `offline` card with the reason. Examples:

- `connection refused`
- `token rejected (401)`
- `timed out connecting`

Hosts:

- **Ordering:** drag hosts in the list to reorder their cards.
- **Pinned models:** give a host pinned models to keep its rows visible while
  they're unloaded.
- **Per-card collapse:** use ▾ on a card to collapse just that host.

**Security model.**

- The agent and the Sharing server are **read-only**. They report model names,
  states, tok/s and GPU stats, and can't load, unload or stop anything.
- STOP and unload in the HUD only ever act on the local machine.
- Use a token (generated by default) on anything reachable beyond your own LAN.
  Clients send it as `Authorization: Bearer <token>`.
- The API is `GET /api/v1/status`, plus a token-free `GET /health`.

## Ollama must be configured to allow it

The HUD can only show what Ollama actually keeps loaded. Two server-side
settings decide that, and neither is the overlay's to control:

| Variable | What it does |
| --- | --- |
| `OLLAMA_MAX_LOADED_MODELS` | Ceiling on co-resident models. Set it to at least the number of models you want warm at once — at `2`, a third model load evicts one of the first two, and the HUD will correctly show only two. |
| `OLLAMA_KEEP_ALIVE` | How long an idle model stays resident. Note the unit suffix: `30s` is thirty *seconds*, `30m` is thirty minutes. A short value looks like models "randomly" dropping to `STOPPED`. |

Both are read by the server at startup, so restart `ollama serve` (or the
Ollama desktop app) after changing them. `WAITING` rows show the countdown
derived from whatever `keep_alive` was in effect for that model, so the row
itself tells you which value actually took hold.

## Model states

Each row carries a state pill, the model's VRAM footprint, its generation
speed, and a trailing detail:

| Pill | Meaning | Trailing detail |
| --- | --- | --- |
| `ACTIVE` (green) | Generating right now | `generating` |
| `WAITING` (amber) | Resident in VRAM, idle, counting down to eviction | `24:21 left` |
| `STOPPED` (grey) | Not resident — unloaded, expired, or never loaded | `not loaded` |

Rows sort active-first, then waiting, then stopped. The header dot and the tray
icon take the aggregate colour, so you can read the whole picture at a glance
or from the notification area with the overlay hidden. The `tok/s` figure is
bright while a model generates and dimmed afterwards — a dimmed number is the
*last* measured speed, not current throughput. Hover any row for a tooltip
naming which detection layer produced its state.

### How state is determined

`/api/ps` deliberately doesn't report whether a model is busy, so state is
resolved in layers, each falling back to the next:

1. **The model's own runner (`/slots`).** Every resident model runs as its own
   `llama-server.exe` subprocess whose command line carries `--port` and
   `--model <blob path>`. Matching that blob against the `FROM <blob path>` in
   `/api/show` gives an exact runner-to-model mapping; the runner's `/slots`
   endpoint then reports `is_processing` per slot. This is also where `tok/s`
   comes from, as the delta of `next_token.n_decoded` over time.
2. **Per-process GPU utilization** (NVML, keyed on the runner PID) — measured
   at 23-69% while a runner generates, dropping to zero within ~2s of
   finishing. Used when `/slots` isn't reachable.
3. **The `expires_at` jump.** Verified on Ollama 0.30.10: `expires_at` is
   frozen for the whole duration of a request and snaps forward to
   `now + keep_alive` the moment it completes, making a forward jump an exact
   "just finished generating" signal. Needs nothing but `/api/ps`, so `ACTIVE`
   still flashes on completion even in the most degraded setup.

Several Ollama tags can share one GGUF blob and differ only in Modelfile
parameters (`qwen3-8b-40k` vs `qwen3-8b-64k` vs `qwen3:8b` are the same weights
at different context sizes). Those are disambiguated by context length, which
`/api/ps` reports and the runner takes as `-c`. Anything still ambiguous is
left unmapped on purpose and falls back to layer 3 — a wrong mapping would
credit one model's generation to another, which is worse than a coarser signal.

## Window controls

The overlay has no title bar — it is nothing but cards. Its two window buttons
sit in the header row of whichever card is on top, and move with it if that
card is hidden:

- **–** — collapse every card to its header row. The window shrinks to a thin
  strip (290px tall down to 80px with two cards) that still shows each card's
  name and status, and still carries the buttons. Click again (`+`) to restore.
  Each card also has its own `▾` to collapse just that one.
- **×** — hide the overlay. It does **not** quit: the app keeps running and the
  tray icon brings it back. Once hidden by hand it stays hidden — a model
  loading won't pop it back up uninvited.

Everything else is on the **right-click menu**, anywhere on the overlay, and in
the tray icon's menu:

- **Settings & dashboard…**
- **Unload local models** — frees VRAM, leaves the server up.
- **Stop local Ollama server…** — asks for confirmation first, since it ends
  every warm model. It unloads first so runners release VRAM cleanly, then
  terminates the runners, `ollama serve`, and the desktop/tray app if present
  (that app supervises the server and would otherwise restart it). Termination
  is graceful first, hard-killed only if a process won't exit. Restart Ollama
  afterwards with `ollama serve` or by launching the Ollama app.
  - **On Linux**, the official installer runs Ollama as `ollama.service` with
    `Restart=always`, so killing the process wouldn't stick. It instead runs
    `systemctl stop ollama`.
  - That needs root. It's tried non-interactively first; if that's refused,
    it goes through `pkexec`, which shows your desktop's password prompt.
- **Collapse / Expand**, **Hide overlay**, **Quit overlay**.

With every card switched off there is nothing to draw and nowhere to put the
buttons, so the overlay hides itself and the tray icon is the way back.

Drag the overlay anywhere by clicking and holding it. Once moved it stays put
instead of re-snapping to its corner; double-click it to snap back.

Only one copy runs at a time:

- A second launch (an autostart entry plus a manual start, say) detects the
  first via a local socket.
- It then asks the running copy to open its settings window and exits, rather
  than stacking a second always-on-top window and a second tray icon. That's
  why the Start-menu entry always "does something".

Note that on
Windows a venv's `pythonw.exe` is a redirector stub that runs the real
interpreter as a child, so one instance legitimately shows up as two
`pythonw.exe` processes in Task Manager.

### Tray icon

The app registers an icon in the notification area. **Windows files new icons
under the "hidden icons" chevron by default** — drag it onto the taskbar itself
if you'd rather have it always visible. Its colour mirrors the header dot and
its tooltip counts active/waiting/stopped models. Click it (or double-click) to
show/hide the overlay; right-click for:

- Show / Hide overlay
- Settings & dashboard…
- Unload local models (keep server) — frees VRAM without ending the session
- Stop local Ollama server — same as the STOP button
- Quit overlay

Stop and unload both run on worker threads, so the HUD never freezes waiting on
HTTP calls or process exits. On a system with no notification area (some
minimal Linux WMs) the tray is skipped and `×` quits instead, since there'd
otherwise be no way to get the overlay back. **GNOME** shows tray icons only
with the "AppIndicator and KStatusNotifierItem Support" extension. Without it,
use the ⚙ button on the HUD.

## What's here

- `app.py` — `OverlayHUD` (a title-bar-less stack of cards; the window buttons
  are re-parented into whichever card is on top),
  `Toast`, `TrayController`, `SharingManager`, and `main()` wiring it all
  together.
- `widgets.py` — `HostCard` (coloured border, name tag, kind badge, tag pills,
  model rows, GPU/VRAM bars) and `ModelRow`, shared by the HUD and the
  dashboard.
- `settings_window.py` — the Settings & dashboard window.
- `settings.py` — JSON settings with defaults, `HostConfig`, and the
  per-host identity (name, colours, tags).
- `remote.py` — `RemotePoller`: polls remote agents and bare Ollama APIs
  concurrently into `HostStatus` objects.
- `hub.py` — `StatusHub`, the latest status of every host. It emits
  online/offline and remote-model events for the toasts.
- `status_server.py` — the read-only passthrough HTTP server (`/api/v1/status`).
- `agent.py` — headless entry point: monitor + status server + LAN beacon.
- `discovery.py` — LAN beacons and the /24 sweep behind **Discover…**.
- `platform_support.py` — the per-OS bits:
  - config paths
  - start-at-login: the HKCU Run key on Windows; XDG autostart and a
    systemd user unit on Linux
  - the XWayland preference for the HUD
- `theme.py` — colours, fonts and formatting helpers.
- `install/install.ps1`, `install/install.sh` — the one-line installers.
- `monitor.py` — `OllamaMonitor`: polls `http://127.0.0.1:11434/api/ps` every
  1.5s and maintains the **roster** — per-model state, eviction countdown, and
  speed — emitting `rosterChanged(rows, gpu_stats)` each tick plus toast
  signals for load/switch/unload transitions. The roster is stickier than
  `/api/ps` on purpose: unloaded models stay as `STOPPED` rows for
  `STOPPED_LINGER_S` (10 minutes) so an unload that happens while you're away
  is still visible when you come back, and pinned models never age out.
- `model_state.py` — runner discovery (`RunnerIndex`), the background
  `SlotsSampler` that polls each runner's `/slots` every 100ms for busy state
  and speed, and `NvmlActivityProbe` for the GPU-utilization fallback.
- `claude_watch.py` — `ClaudeWatcher`: the session registry reader, the desktop
  app detector, and `UsageIndex`, the incremental transcript scanner behind the
  token figures.
- `ollama_control.py` — `unload_models()`, `stop_ollama()`, and the
  `OllamaController` that runs both off the UI thread.
- `gpu_stats.py` — `GPUStats` + `GPUReader` implementations:
  - **NVIDIA** (any OS): `pynvml` (`nvidia-ml-py` package). It's reliable and
    has an identical API on Windows and Linux. All GPUs are aggregated: mean
    utilization, summed VRAM, name shown as `2x RTX …`, so multi-GPU servers
    read correctly. It also supplies per-process SM utilization for
    state-detection layer 2.
  - **AMD Linux**: `amdsmi`/`pyrsmi` if installed (requires ROCm), else
    shells out to the `rocm-smi` CLI.
  - **AMD Windows**: `pyadl` for load% only (it doesn't expose VRAM) plus
    best-effort VRAM via Windows' GPU performance counters (`wmi` package).
    **This path is unverified against real AMD+Windows hardware** — those
    counters are documented to read 0 on a cold query and have a known
    Microsoft accuracy bug on some Windows 10 builds. If it can't get a
    reliable reading, the HUD hides the GPU%/VRAM rows entirely rather than
    showing a misleading number.
  - If no GPU backend is available at all, `NullGPUReader` keeps the app
    running with the GPU%/VRAM rows hidden — model tracking still works, and
    state detection just loses layer 2.
- `token_speed.py` — the old server-log speed parser, kept only as a
  last-resort fallback (see below).

`psutil` is required for runner discovery and for the stop button. Without it
the app still runs: state detection drops to the `expires_at` layer, and on
Windows the stop button falls back to `taskkill`.

AMD extras (`amdsmi`, `pyrsmi`, `pyadl`, `wmi`) aren't pinned in
`requirements.txt` since they're vendor/platform-conditional; install them
manually if you're on AMD hardware and want that fallback path exercised
— on Linux you'll also need ROCm installed for `amdsmi`/`pyrsmi` to work.

## tok/s

Speed is read from each model's own runner via `/slots` (see layer 1 above),
which covers requests from any client with no reconfiguration and attributes
each figure to the right model even with several models warm at once.

The reading is the growth of the runner's decoded-token count over time, so it
needs two observations spanning the generation. That sampling runs on a
**background thread at 100ms**, independent of the HUD's own poll tick — because
a fast model answers entirely between UI-rate polls, which is why small models
previously showed no figure at all. Cost of that cadence, measured here: a
`/slots` response is ~1.2 KB and takes **0.30ms** over a kept-alive connection,
and generation throughput with the sampler running versus stopped came out at
**44.1 vs 44.3 tok/s** — a 0.5% difference, inside run-to-run noise. Only the
sampler thread does this I/O; the UI thread never blocks on it.

Accuracy, measured against Ollama's own reported speed for the same request:

| Reply | Ollama reports | HUD showed | Error |
| --- | --- | --- | --- |
| 644 tokens / 3.1s (1.5B) | 205.4 tok/s | 204.3 | -1% |
| 462 tokens / 5.0s (4B) | 92.9 tok/s | 93.6 | +1% |
| 434 tokens / 4.7s (4B) | 92.0 tok/s | 89.7 | -3% |
| 278 tokens / 1.4s (1.5B) | 203.8 tok/s | 200.1 | -2% |
| 22 tokens / 0.11s (1.5B) | 203.3 tok/s | 136.9 | -33% |
| 10 tokens / 0.06s (1.5B) | 178.6 tok/s | 61.7 | -65% |

**Any reply lasting longer than about a second lands within a few percent.
Replies shorter than ~0.15s read low**, and no sampling rate fixes that
completely: a 60ms generation is shorter than one sampling interval, so the
interval it's measured over is mostly idle time. Those are trivial replies
("hi"), and the figure is still the right order of magnitude — the number to
trust is the one that appears during a normal, multi-second reply.

Three narrower behaviours worth knowing, each there to stop a plausible-looking
but wrong number from being displayed:

- The window is anchored on an observation where tokens already exist, never on
  a slot's task registration — otherwise prompt-processing time is divided into
  the token count, which reported a genuine ~190 tok/s model at 13.8.
- Consecutive growth observations more than 0.35s apart are treated as a
  discontinuity and restart the window, since the model clearly wasn't
  generating for part of that gap (queued, or loading). Without this, two growth
  points straddling a model load reported 4 tok/s.
- While a request is in flight but hasn't yet produced a measurement, the row
  shows no figure rather than the previous request's. Otherwise a stale number
  would be rendered in the live colour — and for a reply too short to ever
  measure, that stale figure would be all the row ever showed.

`token_speed.py` — which parsed `slot print_timing` lines out of Ollama's
`server.log` — is retained only as a fallback for older Ollama builds. **It
produces nothing on Ollama 0.30.10**, which emits no such line; and when
`ollama serve` is started by hand rather than by the desktop app, it doesn't
write `%LOCALAPPDATA%\Ollama\server.log` at all. If you're on an older Ollama
and `/slots` is disabled, that path can still supply a number, with the caveat
that its model attribution is a guess when two models are warm at once.

## Claude sessions and usage

Below the host cards, an optional **CLAUDE** card reports what Claude is doing
on this machine: whether the desktop app is open, every live Claude Code
session with the folder-or-title it's working on, and how many tokens have gone
through them. A session that is generating reads green and says `BUSY`; one
waiting for input reads amber and says `IDLE` — the same colour vocabulary the
model rows use, so the whole overlay is read the same way.

```
CLAUDE                      2 busy · 6 sessions
● Nvidia PhysX Nemo over…                  BUSY
● Ollama app multi-model…                  BUSY
● 23-1d                                    IDLE
+2 more
5h                          1.8M out · 7.3M in
7d                          4.8M out · 23.4M in
```

Hover the card for the full breakdown: cache-read tokens, reply counts, today's
figures, and the top models by output volume.

**By default the card only appears while Ollama has nothing loaded.** The
overlay is for what's running right now, and when models are warm that's the
interesting half; the Claude card steps aside and comes back the moment the
last model unloads. A model that is merely pinned or lingering as `STOPPED`
doesn't count as running, so those don't suppress it.

Two details worth knowing:

- The rule looks at the host cards you actually display. If you've switched the
  local card off, you've opted out of watching Ollama, so a model loading won't
  hide Claude — otherwise the overlay would empty itself.
- Settings → General → *Claude* → "Only when no Ollama model is loaded" turns
  this off, and then both cards are shown together.

### What the numbers are — and what they aren't

**This is measured token volume, not a percentage of your plan.** The
plan-limit percentage is computed server-side and Claude Code only renders it
in its own `/usage` view; nothing on disk caches it, so any percentage shown
here would be invented. For the real figure, run `/usage` inside Claude Code.

What the card counts is what Claude Code wrote to its own transcripts:

| Figure | Meaning |
| --- | --- |
| `out` | Output tokens generated |
| `in` | Fresh input — prompt tokens plus cache *writes* |
| cache-read (tooltip) | Tokens served from the prompt cache, far and away the largest number and the cheapest |

The two windows are `5h` and `7d` because those mirror the windows Claude's own
limits use — a rolling session window and a weekly one.

### Where it reads from

Three local sources, all files Claude Code already maintains. No network calls,
and `~/.claude/.credentials.json` is never opened.

- `~/.claude/sessions/<pid>.json` — the live session registry: pid, working
  directory, a readable name, and a `busy`/`idle` status. It is **not**
  self-cleaning, so every entry is checked against the process table before
  being believed; each file records the process start time, which matches
  psutil's `create_time` exactly, so a recycled pid can't resurrect a session
  that ended days ago.
- The process table, for the desktop app. Electron runs one main process plus
  a dozen-odd helpers, so the card reports *one app* and counts the helpers
  separately rather than claiming fourteen instances.
- `~/.claude/projects/*/*.jsonl` — the transcripts, whose assistant records
  carry a `usage` block and a timestamp.

Transcripts are append-only, so each file is read once and then only from where
the last pass stopped, and the hourly totals are cached in the config folder so
a restart doesn't re-read the backlog. Measured here: a first pass over 160
files took 0.65s, and steady-state passes take about 4ms. Files older than
eight days are never opened.

### Settings

Settings → **General** → *Claude*: turn the card off (which also stops its
watcher thread), set the border colour, how many session rows to show before
`+N more`, and how often to rescan. "Keep the overlay up while a session is
generating" lets a busy Claude session hold the HUD open the way an active
model does — idle sessions deliberately don't, or the overlay would never
auto-hide, since the desktop app tends to be open all day.

Session names come from conversation titles, so they appear on screen; turn the
card off if that's not wanted on a shared display.

## Known limitations

- **`/slots` is not a public API.** It's llama.cpp's endpoint, exposed by the
  runner Ollama bundles. A build started with `--no-slots`, or a future runner
  that drops it, falls through to the NVML and `expires_at` layers — state
  stays truthful but `ACTIVE` may only appear at completion rather than
  throughout a long generation. The row tooltip names the layer in use.
- **`/slots` is polled continuously while models are resident** — every 100ms
  per runner, on a background thread. That is a steady (if tiny) load on the
  runner for a number that is only occasionally looked at: measured at 0.30ms
  per request and 0.5% of generation throughput, but it is not zero. Raise
  `SAMPLE_INTERVAL_S` in `model_state.py` to trade short-reply coverage for
  less chatter. A runner that stops answering is skipped for 5s rather than
  retried every cycle. The UI's own tick (`/api/ps`, roster refresh) stays on
  the UI thread at 300ms while generating and 1.5s otherwise.
- **Claude's plan-limit percentage isn't available locally.** The card shows
  measured token volume instead; `/usage` inside Claude Code is the only place
  the real percentage exists. Token counts also cover only sessions run on this
  machine, under this user account.
- **Claude session detection depends on files Claude Code writes for itself.**
  `~/.claude/sessions/*.json` and the transcript format are internal, not a
  published API; a future Claude Code release could change either, at which
  point the card would show no sessions or stale token totals rather than
  wrong ones. `psutil` is required to verify sessions are live and to detect
  the desktop app.
- **STOP is machine-wide, not instance-scoped.** It matches Ollama processes
  by name, so it stops every local Ollama server, not only the one on the port
  the overlay is watching. That's the intended behaviour for a single local
  instance; if you run two on one machine, use the API rather than this button.
- **Per-model VRAM is approximate.** The number shown per model row comes
  from Ollama's own `size_vram` field, not a live per-process GPU query —
  the device-wide GPU%/VRAM bars are the accurate live numbers.
- **Click-through** (so the overlay doesn't block clicks on windows
  underneath it) isn't wired up. Easy on Windows and X11, largely
  unsupported on Wayland compositors.
- **Transparency** needs a compositing window manager. Windows: works via
  DWM automatically. Linux: GNOME/KDE/Cinnamon composite by default;
  minimal WMs (i3, dwm, etc.) need something like `picom` running, or the
  translucent background renders as solid black.
- **Linux specifics.** Runner discovery matches both process styles:
  - older `llama-server` / `ollama_llama_server` binaries
  - newer `ollama runner --model … --port …` subprocesses, which current Linux
    builds use

  The newer Go-engine runner doesn't serve llama.cpp's `/slots`. For those
  models, state falls back to NVML and then `expires_at`, and live tok/s isn't
  available.

  What has been tested on Linux: the agent, the installers (user service,
  autostart, uninstall) and the GUI modules, all on Ubuntu 24.04 under WSL2.
  The HUD hasn't been tested on a physical Linux desktop yet.

  On Wayland sessions the HUD runs through XWayland. Native Wayland ignores
  window positioning and always-on-top, which a corner HUD depends on.
- **Remote hosts in "Ollama API" mode** show only what `/api/ps` exposes. That
  means no tok/s and no GPU, and ACTIVE appears only as a request finishes. Run
  the agent on that machine for the full picture.
- **Discovery is LAN-only.** Beacons don't cross routers, and the sweep covers
  this machine's /24. Hosts on other subnets or over a VPN are added by address.
- **Packaging** (PyInstaller, for a no-Python-required standalone binary)
  isn't done yet.
