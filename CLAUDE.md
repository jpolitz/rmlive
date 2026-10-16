# rmlive: notes for agents

Joe's tool for projecting his reMarkable Paper Pro live, with ink colors he
controls. README.md has usage, design, and performance numbers; read it
before changing anything.

## At the start of a session

Give Joe a short usage reminder before anything else:

- `./run` starts it (build, deploy if changed, restart, open the viewer);
  `./run --prof` with profiling; `./run stop`, `status`, `log`, `prof`, `bench`.
- Viewer: http://10.11.99.1:8099/. F fullscreen, H hide panel; presets,
  per-ink color pickers, rotate, crop.
- It doesn't survive a tablet reboot; `./run` again.

Then check whether the tablet is reachable (`./run status`) and say so. Keep
it brief and let Joe drive.

## Working on the tablet

- `ssh remarkable` (root@10.11.99.1 over USB). Pass `-o LogLevel=ERROR` to
  hide the post-quantum key-exchange warning.
- "No route to host" while the link is up = macOS Local Network permission
  for the terminal app, not a tablet problem. Tell Joe; you can't fix it.
- The tablet userland is BusyBox: no `timeout`, no `head -c`/`head -2`
  (use `head -n`), `ps` takes no options, awk has no `strtonum`. Parse
  binary dumps on the Mac instead.
- `ssh` inside a `while read` loop eats the loop's stdin; use `ssh -n`.
- The tablet's clock is wrong; don't trust its dates.
- **Read-only toward xochitl.** Never write to `/proc/<pid>/mem`, never
  grab input devices (EVIOCGRAB), don't restart xochitl or reboot without
  asking. Joe uses this live in lectures.
- Root filesystem is ~89% full; binaries and files go in `/home/root`.

## Building

- Rust (Joe reads Rust; keep it Rust). `cargo build --release` cross-compiles
  to static aarch64 musl via `.cargo/config.toml`; `./run` handles the build.
- Dependencies are deliberately minimal (`libc`, `miniz_oxide`). The HTTP
  server is hand-rolled on std; keep it that way unless there's a real need.
- `index.html` is embedded with `include_str!`, so page changes need a
  rebuild + `./run`, and the browser tab needs a reload.

## Performance work

Measure first: `./run --prof`, have Joe reproduce, `./run prof`; or
`./run bench` for the tablet-side steps. Earlier guesses about the
bottleneck were wrong until profiled (it was the browser, then musl's
byte-wise memcmp, then memory bandwidth). Budget: the streamer is capped at
2 encode threads to leave CPU for xochitl; Joe chose that.

## Ideas not done yet

- Pen hover dot: `/dev/input/event2` reports position while hovering; needs
  digitizer→screen coordinate mapping (axes and rotation unknown).
- Raspberry Pi as a laptop-free HDMI box running the viewer.
- Reading only around the pen while drawing, to cut latency further.
- Highlighters / other pen colors haven't been seen yet; semi-transparent
  strokes may need different recolor math.
