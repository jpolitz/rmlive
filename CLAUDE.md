# rmlive: notes for agents

Joe's tool for projecting his reMarkable Paper Pro live, with ink colors he
controls. README.md has usage, design, and performance numbers; read it
before changing anything.

## At the start of a session

Give Joe a short usage reminder before anything else:

- `./run` starts it (build, deploy if changed, restart, open the viewer);
  `./run --prof` with profiling; `./run stop`, `status`, `log`, `prof`, `bench`.
- Viewer: the rmlive Mac app (`./run app` reopens it), or
  http://10.11.99.1:8099/ in a browser. F fullscreen, H hide panel; presets,
  per-ink color pickers, rotate, crop.
- It doesn't survive a tablet reboot; `./run` again.
- `./run put [--replace] [--folder F] FILE.pdf|URL ...` uploads PDFs/EPUBs (e.g. worksheets straight
  from the course site). Needs the USB web interface server up; if it
  isn't, Joe toggles the setting off/on with the cable in. `--folder F`
  puts it in folder F (created if missing). `--replace` swaps out the
  same-named document (within F if given) instead of adding a duplicate.
  Joe's pattern: duplicates pile up in a drafts folder (e.g.
  `CSE11FA26-drafts`), one true copy per document in the class folder.
- `./run ls` lists documents; `./run get NAME|ID|--last ...` downloads them
  as PDFs with ink into the current directory (`-o DIR` elsewhere).
- `./run --sync FOLDER [-o DIR]` (or `./run sync FOLDER [-o DIR]` while
  already running) keeps PDFs of FOLDER's documents current on the Mac
  while plugged in, so they're there after a hurried unplug. `./run sync`
  shows status, `./run sync stop` stops it. Never uploads anywhere.

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
  `put --replace` and folder creation in `put --folder` (Joe chose these,
  Oct 2026): they edit the data dir
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
- The Mac viewer app is Swift (`mac/`), the one non-Rust part: a thin
  WKWebView shell. Native features go in as message types on the
  `rmlive` script handler, with the page falling back to web APIs.

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
  strokes may need different recolor math. (The yellow highlighter exports as
  opaque strokes under the printed page — plain color substitution covers it.)
- Recoloring exported PDFs: done via `get --palette` (Oct 2026) — 'projector'
  or a viewer palette (the page's Copy palette button emits {v, bg, inks};
  Apply merges it into the viewer's settings; rmdocs maps the four stock pen
  primaries through it, black never rewritten). Open: a dark-palette export
  (light ink on a dark background rect).
- Exported PDF structure (verified Oct 2026): per page, /Contents is an array
  [printed-form, ink-stream]; ink is RG stroke ops + rg fills in stock pen
  colors — float32 c/255, sometimes without a leading zero. Stock palette seen:
  green (.56862748 .85490197 .44313726), blue (.1882353 .29019609 .87843138),
  yellow (.98039216 .90588235 .09803922), red (.76 .192 .196), purple
  (.753 .498 .824). Highlighter = same RG ops, opaque, under the printed form.
- Oct 20 2026: tablet hard-rebooted mid-session (sleep/wake + first USB attach
  to a new laptop; USB NIC vanished from the Mac; no pstore). xochitl crash
  recovery said "regions.pdf closed unexpectedly" and re-serialized the open
  page's .rm on reopen — no ink lost; pre/post-crash exports identical modulo
  user edits. If it recurs around deploys, bisect with the old binary.
