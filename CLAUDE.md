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
- `./run put [--replace] FILE.pdf|URL ...` uploads PDFs/EPUBs (e.g. worksheets straight
  from the course site). Needs the USB web interface server up; if it
  isn't, Joe toggles the setting off/on with the cable in. `--replace`
  swaps out the same-named document instead of adding a duplicate.
- `./run ls` lists documents; `./run get NAME|ID|--last ...` downloads them
  as PDFs with ink into the current directory (`-o DIR` elsewhere).

Then check whether the tablet is reachable (`./run status`) and say so. Keep
it brief and let Joe drive.

## Working on the tablet

- `ssh remarkable` (root@10.11.99.1 over USB). Pass `-o LogLevel=ERROR` to
  hide the post-quantum key-exchange warning.
- "No route to host" while the link is up = macOS Local Network permission
  for the terminal app, not a tablet problem. Tell Joe; you can't fix it.
  The permission is per program: Homebrew's python3 got blocked while curl
  and `/usr/bin/python3` worked, so tools do HTTP through curl.
- The tablet userland is BusyBox: no `timeout`, no `head -c`/`head -2`
  (use `head -n`), `ps` takes no options, awk has no `strtonum`. Parse
  binary dumps on the Mac instead.
- `ssh` inside a `while read` loop eats the loop's stdin; use `ssh -n`.
- The tablet's clock is wrong; don't trust its dates.
- **Read-only toward xochitl.** Never write to `/proc/<pid>/mem`, never
  grab input devices (EVIOCGRAB), don't restart xochitl or reboot without
  asking. Joe uses this live in lectures. The one sanctioned exception is
  `put --replace` (Joe chose it, Oct 2026): it edits the data dir
  (`~/.local/share/remarkable/xochitl`) with xochitl stopped, then starts
  it. Back up a document's files before experimenting on it.
- Document format: `<id>.metadata` (name, `parent` folder, type),
  `<id>.content` (page list: formatVersion 1 has `pages` +
  `originalPageCount`, fresh uploads use it; 2 has `cPages` with
  `original`), `<id>.pdf`, `<id>/<page-id>.rm` ink, `<id>.thumbnails/`.
  Folders are `CollectionType` documents; tags live in `.content` `tags`.
  No cloud sync is configured on this tablet.
- Root filesystem is ~89% full; binaries and files go in `/home/root`.
- USB web interface: `xochitl.conf` can say `WebInterfaceEnabled=true`
  while nothing listens on port 80 (seen Oct 2026, fw 3.23). Toggling the
  setting starts the server. It does come back by itself after an xochitl
  restart. Whether it persists across replug/reboot is unknown; note it
  here when observed. (`127.0.0.1:8787` is memfaultd,
  unrelated.)
- Prefer `./run put` (the upload endpoint) over copying files into
  xochitl's data dir over ssh: that needs hand-made metadata and an
  xochitl restart.

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
- Highlighters / other pen colors haven't been seen live yet; semi-transparent
  strokes may need different recolor math. (A yellow highlighter shows up in
  week3-friday's export.)
- Recoloring exported PDFs with the projector palette: the export keeps the
  stock ink colors.
