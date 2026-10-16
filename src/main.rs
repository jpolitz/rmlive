//! rmlive: stream the reMarkable Paper Pro's screen to a browser.
//!
//! xochitl keeps its rendered page as a malloc'd RGBA buffer
//! (1632-px row stride, 1620 visible, 2160 rows). We find it in
//! /proc/<pid>/mem, poll it, and ship changed 64x64 tiles to viewers
//! as deflate-compressed RGB over a long-lived HTTP response.
//!
//! Viewers don't get a queue of diffs. Each one has a set of tiles that
//! changed since its last send plus the newest frame; its connection
//! thread encodes and writes whenever the previous write has finished.
//! A slow viewer skips intermediate frames instead of falling behind.
//!
//! Usage: rmlive [interval_ms] [--prof] | rmlive --bench
//!   --prof writes per-tick/per-send JSONL to /tmp/rmlive-prof.jsonl and
//!   accepts viewer timings at POST /clientstats (-> /tmp/rmlive-client.jsonl).

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::fs::FileExt;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const STRIDE: usize = 1632; // pixels per row in memory
const VIS_W: usize = 1620; // visible pixels per row
const H: usize = 2160;
const FRAME_BYTES: usize = STRIDE * H * 4;
// glibc chunk size (incl. 16-byte header) of the frame allocation
const CHUNK_SIZE: u64 = 0xD7_3000;
const PAGE: u64 = 4096;
const TILE: usize = 64;
const BAND: usize = 16; // rows read+hashed per syscall; small enough to stay in cache
const TILES_X: usize = VIS_W.div_ceil(TILE);
const TILES_Y: usize = H.div_ceil(TILE);
const N_TILES: usize = TILES_X * TILES_Y;

const PORT: u16 = 8099;
const INDEX_HTML: &str = include_str!("index.html");

type Frame = Arc<Vec<u8>>;

/// What one viewer still needs: tiles changed since its last send, and
/// the newest frame to take them from.
struct Pending {
    dirty: Vec<bool>,
    frame: Option<Frame>,
    since: Option<Instant>, // when the oldest unsent change appeared
    closed: bool,
}

struct Viewer {
    state: Mutex<Pending>,
    wake: Condvar,
}

#[derive(Default)]
struct Hub {
    viewers: Vec<Arc<Viewer>>,
    latest: Option<Frame>,
}

// ---------- profiling ----------

static START: OnceLock<Instant> = OnceLock::new();
static PROF: OnceLock<Mutex<BufWriter<File>>> = OnceLock::new();

fn now_ms() -> u32 {
    START.get().unwrap().elapsed().as_millis() as u32
}

fn prof(line: String) {
    if let Some(p) = PROF.get() {
        let _ = writeln!(p.lock().unwrap(), "{line}");
    }
}

fn prof_flush() {
    if let Some(p) = PROF.get() {
        let _ = p.lock().unwrap().flush();
    }
}

fn us(d: Duration) -> u128 {
    d.as_micros()
}

/// Pen events since the last tick, and whether the pen is touching.
static PEN_EVENTS: AtomicU32 = AtomicU32::new(0);
static PEN_DOWN: AtomicBool = AtomicBool::new(false);

/// Watch the marker digitizer (no grab, so xochitl still gets everything).
fn pen_loop() {
    const EV_KEY: u16 = 1;
    const EV_ABS: u16 = 3;
    const BTN_TOUCH: u16 = 330;
    let Ok(mut f) = File::open("/dev/input/event2") else {
        eprintln!("pen: can't open /dev/input/event2");
        return;
    };
    // struct input_event on aarch64: timeval (16) + type u16 + code u16 + value i32
    let mut ev = [0u8; 24];
    while f.read_exact(&mut ev).is_ok() {
        let ty = u16::from_le_bytes([ev[16], ev[17]]);
        let code = u16::from_le_bytes([ev[18], ev[19]]);
        let value = i32::from_le_bytes(ev[20..24].try_into().unwrap());
        if ty == EV_ABS {
            PEN_EVENTS.fetch_add(1, Ordering::Relaxed);
        } else if ty == EV_KEY && code == BTN_TOUCH {
            PEN_DOWN.store(value != 0, Ordering::Relaxed);
            prof(format!("{{\"k\":\"pen\",\"ms\":{},\"down\":{}}}", now_ms(), value != 0));
        }
    }
}

fn main() {
    START.set(Instant::now()).unwrap();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--bench") {
        bench();
        return;
    }
    // ~21 ms of read+hash per tick; 33 ms leaves headroom for xochitl
    let interval = args.iter().find_map(|s| s.parse::<u64>().ok()).unwrap_or(33);
    if args.iter().any(|a| a == "--prof") {
        let f = File::create("/tmp/rmlive-prof.jsonl").expect("prof file");
        PROF.set(Mutex::new(BufWriter::new(f))).ok();
        let _ = File::create("/tmp/rmlive-client.jsonl");
        thread::spawn(pen_loop);
    }
    let hub = Arc::new(Mutex::new(Hub::default()));

    let h = hub.clone();
    thread::spawn(move || capture_loop(h, Duration::from_millis(interval)));

    let listener = TcpListener::bind(("0.0.0.0", PORT)).expect("bind");
    eprintln!(
        "rmlive: http://10.11.99.1:{PORT}/ (poll every {interval} ms, prof {})",
        PROF.get().is_some()
    );
    for stream in listener.incoming().flatten() {
        let h = hub.clone();
        thread::spawn(move || {
            if let Err(e) = handle(stream, h) {
                eprintln!("conn: {e}");
            }
        });
    }
}

// ---------- locating the frame ----------

fn xochitl_pid() -> Option<u32> {
    fs::read_dir("/proc").ok()?.flatten().find_map(|e| {
        let pid: u32 = e.file_name().to_str()?.parse().ok()?;
        let comm = fs::read_to_string(format!("/proc/{pid}/comm")).ok()?;
        (comm.trim() == "xochitl").then_some(pid)
    })
}

/// Addresses of pixel data for every RGBA frame-sized chunk in xochitl.
fn find_frames(pid: u32, mem: &File) -> io::Result<Vec<u64>> {
    let maps = fs::read_to_string(format!("/proc/{pid}/maps"))?;
    let mut found = Vec::new();
    for line in maps.lines() {
        let mut parts = line.split_whitespace();
        let (Some(range), Some(perms)) = (parts.next(), parts.next()) else { continue };
        if !perms.starts_with("rw") {
            continue;
        }
        let Some((a, b)) = range.split_once('-') else { continue };
        let (Ok(start), Ok(end)) = (u64::from_str_radix(a, 16), u64::from_str_radix(b, 16)) else { continue };
        if end - start < CHUNK_SIZE {
            continue;
        }
        let mut addr = start;
        while addr + CHUNK_SIZE <= end {
            if is_color_frame(mem, addr) {
                found.push(addr + 16);
            }
            addr += PAGE;
        }
    }
    Ok(found)
}

/// Chunk header at `chunk` has our size, and the first pixels are opaque
/// (the gray e-ink buffer has the same size but alpha 0).
fn is_color_frame(mem: &File, chunk: u64) -> bool {
    let mut hdr = [0u8; 16];
    if mem.read_exact_at(&mut hdr, chunk).is_err() {
        return false;
    }
    let size = u64::from_le_bytes(hdr[8..16].try_into().unwrap());
    if size & !7 != CHUNK_SIZE {
        return false;
    }
    let mut px = [0u8; 256];
    if mem.read_exact_at(&mut px, chunk + 16).is_err() {
        return false;
    }
    px.chunks_exact(4).all(|p| p[3] == 255)
}

// ---------- capture ----------

struct Source {
    pid: u32,
    mem: File,
    addr: u64,
}

fn open_source() -> Option<Source> {
    let pid = xochitl_pid()?;
    let mem = File::open(format!("/proc/{pid}/mem")).ok()?;
    let frames = find_frames(pid, &mem).ok()?;
    eprintln!(
        "xochitl pid {pid}: {} color frame(s): {:x?}",
        frames.len(),
        frames
    );
    let addr = *frames.first()?;
    Some(Source { pid, mem, addr })
}

fn still_valid(src: &Source) -> bool {
    fs::metadata(format!("/proc/{}", src.pid)).is_ok() && is_color_frame(&src.mem, src.addr - 16)
}

fn capture_loop(hub: Arc<Mutex<Hub>>, interval: Duration) {
    // `cur` is read into; `prev` is the last published frame. Viewers hold
    // Arcs to published frames, so we only reuse a buffer nobody else holds.
    // Change detection compares per-tile hashes, not pixels, so only the
    // new frame is ever scanned.
    let mut cur: Frame = Arc::new(vec![0u8; FRAME_BYTES]);
    let mut prev: Option<Frame> = None;
    let mut hashes = vec![0u64; N_TILES];
    let mut prev_hashes: Option<Vec<u64>> = None;
    let mut src: Option<Source> = None;
    let mut last_check = Instant::now();
    let mut last_flush = Instant::now();

    loop {
        let tick = Instant::now();

        if src.is_none() {
            src = open_source();
            prev_hashes = None;
            if src.is_none() {
                thread::sleep(Duration::from_secs(1));
                continue;
            }
        }
        let s = src.as_ref().unwrap();
        if last_check.elapsed() > Duration::from_secs(2) {
            last_check = Instant::now();
            if !still_valid(s) {
                eprintln!("frame went away; rescanning");
                src = None;
                continue;
            }
        }
        let n_viewers = hub.lock().unwrap().viewers.len();
        if n_viewers == 0 {
            prev_hashes = None;
            hub.lock().unwrap().latest = None;
            thread::sleep(interval);
            continue;
        }

        let t = Instant::now();
        if Arc::get_mut(&mut cur).is_none() {
            cur = Arc::new(vec![0u8; FRAME_BYTES]);
        }
        let buf = Arc::get_mut(&mut cur).unwrap();
        if read_and_hash(s, buf, &mut hashes, BAND).is_err() {
            src = None;
            continue;
        }
        let read_t = t.elapsed(); // read + hash, fused
        let dirty: Vec<bool> = match &prev_hashes {
            Some(p) => hashes.iter().zip(p).map(|(a, b)| a != b).collect(),
            None => vec![true; N_TILES],
        };
        prev_hashes = Some(hashes.clone());
        let n_dirty = dirty.iter().filter(|&&d| d).count();

        let frame = cur.clone();
        {
            let mut h = hub.lock().unwrap();
            h.latest = Some(frame.clone());
            h.viewers.retain(|v| !v.state.lock().unwrap().closed);
            if n_dirty > 0 {
                for v in &h.viewers {
                    let mut p = v.state.lock().unwrap();
                    for (d, &n) in p.dirty.iter_mut().zip(&dirty) {
                        *d |= n;
                    }
                    p.frame = Some(frame.clone());
                    p.since.get_or_insert(tick);
                    v.wake.notify_one();
                }
            }
        }
        // recycle the previous frame's buffer if no viewer still holds it
        cur = prev.take().unwrap_or_else(|| Arc::new(vec![0u8; FRAME_BYTES]));
        prev = Some(frame);

        let work = tick.elapsed();
        let pen_ev = PEN_EVENTS.swap(0, Ordering::Relaxed);
        prof(format!(
            "{{\"k\":\"tick\",\"ms\":{},\"read_us\":{},\"dirty\":{},\"viewers\":{},\"work_us\":{},\"pen_ev\":{},\"pen_down\":{}}}",
            now_ms(), us(read_t), n_dirty, n_viewers, us(work),
            pen_ev, PEN_DOWN.load(Ordering::Relaxed)
        ));
        if last_flush.elapsed() > Duration::from_secs(1) {
            last_flush = Instant::now();
            prof_flush();
        }
        if let Some(rest) = interval.checked_sub(work) {
            thread::sleep(rest);
        }
    }
}

type Rect = (usize, usize, usize, usize); // x, y, w, h

fn tile_rect(i: usize) -> Rect {
    let (x, y) = ((i % TILES_X) * TILE, (i / TILES_X) * TILE);
    (x, y, TILE.min(VIS_W - x), TILE.min(H - y))
}

/// One copy straight from xochitl's address space (/proc/<pid>/mem goes
/// through a kernel bounce page).
fn vm_read(pid: u32, addr: u64, buf: &mut [u8]) -> io::Result<()> {
    let local = libc::iovec { iov_base: buf.as_mut_ptr().cast(), iov_len: buf.len() };
    let remote = libc::iovec { iov_base: addr as *mut libc::c_void, iov_len: buf.len() };
    // SAFETY: `local` covers exactly `buf`, which we hold mutably; the
    // kernel validates the remote range and fails instead of faulting.
    let n = unsafe { libc::process_vm_readv(pid as libc::pid_t, &local, 1, &remote, 1, 0) };
    if n < 0 {
        return Err(io::Error::last_os_error());
    }
    if n as usize != buf.len() {
        return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "short process_vm_readv"));
    }
    Ok(())
}

const K: u64 = 0x9E37_79B9_7F4A_7C15;

/// Fold `bytes` into `h`. Four independent lanes keep the multiplier busy.
/// Each step is a bijection of the lane state, so changing any single word
/// always changes the result.
fn mix(h: u64, bytes: &[u8]) -> u64 {
    let mut l = [h, h ^ 0x51, h ^ 0xA3, h ^ 0xF5];
    let mut c = bytes.chunks_exact(32);
    for b in &mut c {
        for (i, lane) in l.iter_mut().enumerate() {
            let w = u64::from_ne_bytes(b[i * 8..i * 8 + 8].try_into().unwrap());
            *lane = (*lane ^ w).wrapping_mul(K);
        }
    }
    for b in c.remainder().chunks(8) {
        let mut w = [0u8; 8];
        w[..b.len()].copy_from_slice(b);
        l[0] = (l[0] ^ u64::from_ne_bytes(w)).wrapping_mul(K);
    }
    (l[0] ^ l[1].rotate_left(17) ^ l[2].rotate_left(31) ^ l[3].rotate_left(47)).wrapping_mul(K)
}

/// Read `band` rows at a time and hash each tile's slice of them while
/// it's still in cache. Fills `hashes` (one per tile) for the whole frame.
/// `band` must divide TILE.
fn read_and_hash(src: &Source, cur: &mut [u8], hashes: &mut [u64], band: usize) -> io::Result<()> {
    for y0 in (0..H).step_by(band) {
        let rows = band.min(H - y0);
        let (off, len) = (y0 * STRIDE * 4, rows * STRIDE * 4);
        vm_read(src.pid, src.addr + off as u64, &mut cur[off..off + len])?;
        let ty = y0 / TILE;
        let tile_row = &mut hashes[ty * TILES_X..(ty + 1) * TILES_X];
        if y0 % TILE == 0 {
            tile_row.fill(0);
        }
        for row in y0..y0 + rows {
            for (tx, h) in tile_row.iter_mut().enumerate() {
                let (x, _, w, _) = tile_rect(ty * TILES_X + tx);
                let o = (row * STRIDE + x) * 4;
                *h = mix(*h, &cur[o..o + w * 4]);
            }
        }
    }
    Ok(())
}

/// `rmlive --bench`: time the read, change-detection, and encode steps
/// against the live frame, and sanity-check the tile hash.
fn bench() {
    let Some(s) = open_source() else {
        eprintln!("bench: no frame found");
        return;
    };
    let mut a = vec![0u8; FRAME_BYTES];
    const N: usize = 30;
    let time = |name: &str, f: &mut dyn FnMut()| {
        let mut v: Vec<f64> = (0..N)
            .map(|_| {
                let t = Instant::now();
                f();
                t.elapsed().as_secs_f64() * 1000.0
            })
            .collect();
        v.sort_by(|x, y| x.partial_cmp(y).unwrap());
        eprintln!("{name:30} p50 {:6.2} ms   p90 {:6.2} ms", v[N / 2], v[N * 9 / 10]);
    };
    time("pread /proc/pid/mem", &mut || s.mem.read_exact_at(&mut a, s.addr).unwrap());
    time("process_vm_readv, whole frame", &mut || vm_read(s.pid, s.addr, &mut a).unwrap());
    let mut hashes = vec![0u64; N_TILES];
    time(&format!("read+hash, {BAND}-row bands"), &mut || {
        read_and_hash(&s, &mut a, &mut hashes, BAND).unwrap()
    });
    // sanity: hashes are deterministic, and one changed byte flips its tile
    let mut h2 = vec![0u64; N_TILES];
    read_and_hash(&s, &mut a, &mut hashes, BAND).unwrap();
    read_and_hash(&s, &mut a, &mut h2, BAND).unwrap();
    let same = hashes.iter().zip(&h2).filter(|(x, y)| x == y).count();
    let mut b2 = a.clone();
    let probe = (1000 * STRIDE + 700) * 4;
    b2[probe] ^= 1;
    eprintln!("hash stability: {same}/{N_TILES} tiles equal across two reads (idle page)");
    let t_of = |buf: &[u8], i: usize| {
        let (x, y, w, h) = tile_rect(i);
        (y..y + h).fold(0, |acc, row| {
            let o = (row * STRIDE + x) * 4;
            mix(acc, &buf[o..o + w * 4])
        })
    };
    let ti = (1000 / TILE) * TILES_X + 700 / TILE;
    eprintln!("1-bit flip in tile {ti} detected: {}", t_of(&a, ti) != t_of(&b2, ti));
    let tiles: Vec<Rect> = (0..N_TILES).map(tile_rect).collect();
    time("encode all tiles, 1 thread", &mut || {
        std::hint::black_box(encode(&a, &tiles, 0));
    });
    time("encode all tiles, 2 threads", &mut || {
        std::hint::black_box(encode_parallel(&a, &tiles, 0));
    });
}

/// Message: 12-byte header (u32 LE payload length, seq, server ms), then
/// raw-deflate of u16 count, then per tile: u16 x, y, w, h and w*h RGB bytes.
/// Returns the message and the uncompressed payload size.
fn encode(frame: &[u8], tiles: &[Rect], seq: u32) -> (Vec<u8>, usize) {
    let mut raw = Vec::with_capacity(2 + tiles.len() * (8 + TILE * TILE * 3));
    raw.extend_from_slice(&(tiles.len() as u16).to_le_bytes());
    for &(x, y, w, h) in tiles {
        for v in [x, y, w, h] {
            raw.extend_from_slice(&(v as u16).to_le_bytes());
        }
        for row in y..y + h {
            let o = (row * STRIDE + x) * 4;
            for px in frame[o..o + w * 4].chunks_exact(4) {
                raw.extend_from_slice(&px[..3]);
            }
        }
    }
    let z = miniz_oxide::deflate::compress_to_vec(&raw, 1);
    let mut bytes = Vec::with_capacity(12 + z.len());
    bytes.extend_from_slice(&(z.len() as u32).to_le_bytes());
    bytes.extend_from_slice(&seq.to_le_bytes());
    bytes.extend_from_slice(&now_ms().to_le_bytes());
    bytes.extend_from_slice(&z);
    (bytes, raw.len())
}

/// Big updates (zoom, page turn) compress in two halves on two threads and
/// go out as two messages; the viewer applies each independently. Two
/// leaves the other cores to xochitl.
fn encode_parallel(frame: &[u8], tiles: &[Rect], seq: u32) -> Vec<(Vec<u8>, usize)> {
    if tiles.len() < 64 {
        return vec![encode(frame, tiles, seq)];
    }
    let (left, right) = tiles.split_at(tiles.len() / 2);
    thread::scope(|s| {
        let other = s.spawn(|| encode(frame, right, seq));
        let mine = encode(frame, left, seq);
        vec![mine, other.join().unwrap()]
    })
}

// ---------- http ----------

fn stream_to(out: &mut TcpStream, hub: &Arc<Mutex<Hub>>) -> io::Result<()> {
    let viewer = {
        let mut h = hub.lock().unwrap();
        let v = Arc::new(Viewer {
            state: Mutex::new(Pending {
                dirty: vec![true; N_TILES], // first send is a keyframe
                frame: h.latest.clone(),
                since: Some(Instant::now()),
                closed: false,
            }),
            wake: Condvar::new(),
        });
        h.viewers.push(v.clone());
        v
    };
    let mut seq = 0u32;
    let result = loop {
        // wait for something to send, then take it all at once
        let (frame, tiles, since) = {
            let mut p = viewer.state.lock().unwrap();
            while p.frame.is_none() {
                p = viewer.wake.wait(p).unwrap();
            }
            let tiles: Vec<Rect> = (0..N_TILES).filter(|&i| p.dirty[i]).map(tile_rect).collect();
            p.dirty.iter_mut().for_each(|d| *d = false);
            (p.frame.take().unwrap(), tiles, p.since.take())
        };
        seq += 1;
        let t = Instant::now();
        let parts = encode_parallel(&frame, &tiles, seq);
        drop(frame);
        let enc_t = t.elapsed();
        let t = Instant::now();
        if let Err(e) = parts.iter().try_for_each(|(bytes, _)| out.write_all(bytes)) {
            break Err(e);
        }
        prof(format!(
            "{{\"k\":\"send\",\"ms\":{},\"seq\":{},\"tiles\":{},\"parts\":{},\"age_us\":{},\"enc_us\":{},\"write_us\":{},\"raw_b\":{},\"z_b\":{}}}",
            now_ms(), seq, tiles.len(), parts.len(),
            since.map(|s| us(s.elapsed())).unwrap_or(0),
            us(enc_t), us(t.elapsed()),
            parts.iter().map(|p| p.1).sum::<usize>(),
            parts.iter().map(|p| p.0.len()).sum::<usize>()
        ));
    };
    viewer.state.lock().unwrap().closed = true;
    result
}

fn handle(stream: TcpStream, hub: Arc<Mutex<Hub>>) -> io::Result<()> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request = String::new();
    reader.read_line(&mut request)?;
    let mut content_length = 0usize;
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header)? == 0 || header == "\r\n" {
            break;
        }
        if let Some((k, v)) = header.split_once(':') {
            if k.eq_ignore_ascii_case("content-length") {
                content_length = v.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut parts = request.split_whitespace();
    let method = parts.next().unwrap_or("GET");
    let path = parts.next().unwrap_or("/");
    let mut out = stream;
    match (method, path) {
        ("GET", "/" | "/index.html") => {
            write!(
                out,
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\
                 Content-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
                INDEX_HTML.len()
            )?;
            out.write_all(INDEX_HTML.as_bytes())
        }
        ("GET", p) if p.starts_with("/stream") => {
            out.set_nodelay(true)?;
            out.write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\
                  Cache-Control: no-store\r\nConnection: close\r\n\r\n",
            )?;
            stream_to(&mut out, &hub)
        }
        ("POST", "/clientstats") => {
            let mut body = vec![0u8; content_length.min(1 << 20)];
            reader.read_exact(&mut body)?;
            if PROF.get().is_some() {
                let mut f = OpenOptions::new().append(true).open("/tmp/rmlive-client.jsonl")?;
                f.write_all(&body)?;
                f.write_all(b"\n")?;
            }
            out.write_all(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        }
        _ => out.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"),
    }
}
