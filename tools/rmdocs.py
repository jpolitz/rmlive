"""Documents on the tablet via the USB web interface. Called by `./run`.

  rmdocs.py ls
  rmdocs.py get [-o DIR] [--palette projector|FILE] (NAME | ID | --last) ...
  rmdocs.py put [--replace] [--folder PATH] FILE ...

get: NAME matches exactly (case-insensitive, ".pdf" optional) if anything
does, otherwise as a substring; every match is downloaded as a PDF with ink.
--palette remaps ink colors: 'projector' (the viewer's preset) or a palette
file/JSON from the viewer's Copy palette button.
--palette projector remaps stock ink colors to the viewer's Projector preset.

put: uploads PDFs/EPUBs. --folder puts them in PATH ("A/B" nests), creating
folders as needed. --replace makes FILE the one document with that name (in
--folder if given, else anywhere): if the old one has ink, the new PDF goes
into the old document instead (ink stays on its pages), which needs the same
page count. The web interface can't create folders, move, or delete, so
those edit xochitl's files over ssh with xochitl stopped: one restart per run.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
import zlib

WEB = "http://10.11.99.1"
HOST = os.environ.get("RM_HOST", "remarkable")
DATA = "/home/root/.local/share/remarkable/xochitl"

# Shell commands to run in DATA with xochitl stopped, all in one restart.
EDITS = []

# Exported PDFs draw ink as stroke-color ops (RG) in the stock pen colors,
# float32-rounded c/255 and sometimes without a leading zero. The viewer's
# stock pen keys (src/index.html PRESETS) map to those export colors. Black is
# deliberately absent: printed black strokes are indistinguishable from black
# ink, so black mappings are never applied (a dark-palette export would need
# ink-layer-only rewriting). Only RG ops matching a stock color exactly (within
# float32 slop) are rewritten: the printed page rides along as a form XObject
# with its own colors and passes through. The highlighter exports as opaque
# strokes under the page, so the same substitution covers it.
VIEWER_STOCK = {
    "#00ff00": (145 / 255, 218 / 255, 113 / 255),   # green
    "#0000ff": (48 / 255, 74 / 255, 224 / 255),     # blue
    "#ffff00": (250 / 255, 231 / 255, 25 / 255),    # yellow (also the highlighter)
    "#ff0000": (194 / 255, 49 / 255, 50 / 255),     # red
    # purple (.753 .498 .824) seen once; no stock pure-primary key known yet
}

# The viewer's Projector preset (src/index.html PRESETS.projector). bg is not
# used for exports: the printed page is the background.
PROJECTOR = {"#000000": "#000000", "#00ff00": "#1a7f37", "#ffff00": "#b35c00", "#0000ff": "#1f4fd1"}


def palette_table(inks):
    """Stock export color -> target RGB floats, from a viewer palette {stockKey: chosenHex}.
    Unmapped pens and unknown keys are skipped (they pass through stock)."""
    table = {}
    for k, v in inks.items():
        k, v = str(k).lower(), str(v).lower()
        if k in VIEWER_STOCK and v != k:
            m = re.fullmatch(r"#?([0-9a-f]{6})", v)
            if m:
                n = int(m.group(1), 16)
                table[VIEWER_STOCK[k]] = ((n >> 16) / 255, (n >> 8 & 255) / 255, (n & 255) / 255)
    return table


INK_OP = re.compile(rb"(\.?\d+\.?\d*) (\.?\d+\.?\d*) (\.?\d+\.?\d*) RG")
INK_STREAM = re.compile(rb"<<[^<>]*/Filter ?/FlateDecode[^<>]*/Length (\d+)[^<>]*>>\s*stream\r?\n")


def recolor_pdf(path, table):
    """Remap stock ink colors in an exported PDF per `table` (see palette_table),
    in place. Returns the number of stroke ops rewritten."""
    def remap(m):
        rgb = tuple(float(g) for g in m.groups())
        for src, dst in table.items():
            if max(abs(a - b) for a, b in zip(rgb, src)) <= 0.001:
                hits[0] += 1
                return b"%.6f %.6f %.6f RG" % dst
        return m.group(0)

    hits = [0]

    with open(path, "rb") as f:
        data = f.read()
    out = bytearray()
    pos = 0
    n = 0
    for m in INK_STREAM.finditer(data):
        length = int(m.group(1))
        start, data_start = m.start(), m.end()
        data_end = data_start + length
        if data_end > len(data):
            continue
        try:
            content = zlib.decompress(data[data_start:data_end])
        except zlib.error:
            continue
        new_content = INK_OP.sub(remap, content)
        if hits[0] > n:
            new_data = zlib.compress(new_content, 9)
            # data[start:data_start] is the dict plus the "stream\r\n" separator;
            # update /Length inside it, keep the separator verbatim.
            new_dict = re.sub(rb"/Length \d+", b"/Length %d" % len(new_data), data[start:data_start])
            out += data[pos:start]
            out += new_dict + new_data
            pos = data_end  # the \r\n before endstream and beyond stay verbatim
        n = hits[0]
    if pos:
        out += data[pos:]
        with open(path, "wb") as f:
            f.write(out)
    return n


# HTTP goes through curl rather than urllib: macOS grants Local Network access
# per program, and curl has it whenever the terminal does, while e.g.
# Homebrew's python3 can be blocked separately ("No route to host").
def curl(*args, timeout=10):
    r = subprocess.run(["curl", "-sS", "-m", str(timeout), "-H", f"Referer: {WEB}/", *args],
                       capture_output=True)
    if r.returncode != 0:
        sys.exit(f"curl {args[-1]}: {r.stderr.decode().strip()}")
    return r.stdout


def fetch(path, timeout=10):
    return curl("-f", WEB + path, timeout=timeout)


def ssh(cmd):
    return subprocess.run(["ssh", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR", HOST, cmd],
                          check=True, capture_output=True, text=True).stdout


def set_parent(doc_id, parent):
    EDITS.append(f"sed -i 's/\"parent\": \"[^\"]*\"/\"parent\": \"{parent}\"/' {doc_id}.metadata")


def upload(path, folder=""):
    """POST to /upload like the web page does, into `folder` (an ID, "" = top
    level). Returns the new document's ID."""
    if folder:
        # The web page uploads into the folder it's showing; browsing there
        # first may be enough. If not, the document gets moved afterwards.
        # A folder created in this run doesn't exist yet, hence no check=True.
        subprocess.run(["curl", "-s", "-m", "5", "-o", "/dev/null", "-H", f"Referer: {WEB}/",
                        f"{WEB}/documents/{folder}"])
    before = {d["ID"] for _, d in walk()}
    name = os.path.basename(path)
    kind = "application/epub+zip" if name.lower().endswith(".epub") else "application/pdf"
    code = curl("-o", "/dev/null", "-w", "%{http_code}", "-H", f"Origin: {WEB}",
                "-F", f"file=@{path};type={kind}", WEB + "/upload", timeout=60)
    if code != b"201":
        raise RuntimeError(f"upload failed ({code.decode()})")
    for _ in range(20):
        new = [d for _, d in walk() if d["ID"] not in before]
        if new:
            if new[0].get("Parent", "") != folder:
                print(f"{name}: moving into its folder (needs an xochitl restart)")
                set_parent(new[0]["ID"], folder)
            return new[0]["ID"]
        time.sleep(0.5)
    raise RuntimeError("uploaded but it never appeared in the document list")


def pdf_pages(path):
    """Page count via poppler's pdfinfo, or None if that isn't available."""
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return None
    for line in out.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return None


def apply_edits():
    """Run EDITS with xochitl stopped, then start it and wait for the web interface."""
    if not EDITS:
        return
    print("restarting xochitl to apply changes...")
    script = " && ".join(EDITS)
    ssh(f"systemctl stop xochitl && cd {DATA} && {{ {script} ; }}; systemctl start xochitl")
    EDITS.clear()
    for _ in range(60):
        if subprocess.run(["curl", "-sf", "-m", "2", "-o", "/dev/null", WEB + "/documents/"]).returncode == 0:
            return
        time.sleep(0.5)
    print("xochitl restarted but the USB web interface isn't answering yet;"
          " toggle it off/on if it stays down", file=sys.stderr)


def folders(parent="", prefix=""):
    """{path: ID} for every folder."""
    out = {}
    for d in json.loads(fetch(f"/documents/{parent}" if parent else "/documents/")):
        if d["Type"] == "CollectionType":
            path = prefix + d["VissibleName"]
            out[path] = d["ID"]
            out.update(folders(d["ID"], path + "/"))
    return out


def ensure_folder(path):
    """ID of folder `path`, queueing creation of any missing parts."""
    known = folders()
    parent, sofar = "", ""
    for part in [p for p in path.split("/") if p]:
        sofar = f"{sofar}/{part}" if sofar else part
        if sofar in known:
            parent = known[sofar]
            continue
        fid, now = str(uuid.uuid4()), str(int(time.time() * 1000))
        meta = {"createdTime": now, "lastModified": now, "new": False, "parent": parent,
                "pinned": False, "source": "", "type": "CollectionType", "visibleName": part}
        EDITS.append(f"printf %s {shlex.quote(json.dumps(meta, indent=4))} > {fid}.metadata && "
                     f"printf %s {shlex.quote(json.dumps({'tags': []}))} > {fid}.content")
        print(f"creating folder: {sofar}")
        parent = fid
    return parent


def replace(path, docs, folder):
    """Queue making `path` the one document with its name (within `folder`
    if not None). Returns a message."""
    name = os.path.basename(path)
    olds = [(p, d) for p, d in docs if stem(d["VissibleName"]).lower() == stem(name).lower()
            and (folder is None or d.get("Parent", "") == folder)]
    if not olds:
        upload(path, folder or "")
        return f"uploaded: {name} (nothing to replace)"
    if len(olds) > 1:
        listing = "\n".join(f"  {p}  [{d['ID']}]" for p, d in olds)
        raise RuntimeError(f"{len(olds)} documents named {stem(name)!r}, not sure which:\n{listing}")
    old_path, old = olds[0]
    oid = old["ID"]
    info = json.loads(ssh(f"cd {DATA} && printf '{{\"ink\": %s, \"content\": ' "
                          f"$(ls {oid}/*.rm 2>/dev/null | wc -l) && cat {oid}.content && printf '}}'"))
    if info["ink"] == 0:
        upload(path, old.get("Parent", ""))
        EDITS.append(f"rm -rf {oid} {oid}.*")
        return f"replaced: {old_path}"
    # Ink lives on page entries that point at original PDF pages, so swap the PDF
    # underneath them, which only makes sense if the pages line up.
    c = info["content"]  # formatVersion 2 has cPages; 1 (fresh uploads) has originalPageCount
    have = c["cPages"]["original"]["value"] if "cPages" in c else c.get("originalPageCount")
    want = pdf_pages(path)
    if want != have:
        raise RuntimeError(f"{old_path} has ink and {have} pages, the new PDF has "
                           f"{want if want is not None else 'an unknown number of'} pages; refusing so "
                           f"the ink doesn't land on the wrong pages. Plain ./run put keeps both.")
    subprocess.run(["scp", "-q", "-o", "LogLevel=ERROR", path, f"{HOST}:{DATA}/{oid}.pdf.new"], check=True)
    now = int(time.time() * 1000)
    size = os.path.getsize(path)
    EDITS.append(f"mv {oid}.pdf.new {oid}.pdf && rm -rf {oid}.thumbnails && "
                 f"sed -i 's/\"lastModified\": \"[0-9]*\"/\"lastModified\": \"{now}\"/' {oid}.metadata && "
                 f"sed -i 's/\"sizeInBytes\": \"[0-9]*\"/\"sizeInBytes\": \"{size}\"/' {oid}.content")
    return f"replaced: {old_path} (kept ink on {info['ink']} page(s))"


def put(args):
    replacing, folder, files = False, None, []
    while args:
        a = args.pop(0)
        if a == "--replace":
            replacing = True
        elif a == "--folder":
            if not args:
                sys.exit("--folder needs a name")
            folder = args.pop(0)
        else:
            files.append(a)
    if not files:
        sys.exit("usage: ./run put [--replace] [--folder PATH] FILE.pdf|URL ...")
    fid = ensure_folder(folder) if folder else None
    docs = list(walk())
    failed, done = False, []
    for f in files:
        name = os.path.basename(f)
        try:
            if not name.lower().endswith((".pdf", ".epub")):
                raise RuntimeError("only .pdf and .epub")
            if not os.path.isfile(f):
                raise RuntimeError("no such file")
            if replacing:
                if not name.lower().endswith(".pdf"):
                    raise RuntimeError("--replace is PDF-only")
                done.append(replace(f, docs, fid))
            else:
                upload(f, fid or "")
                done.append(f"uploaded: {name}" + (f" -> {folder}/" if folder else ""))
        except (RuntimeError, subprocess.CalledProcessError) as e:
            print(f"FAILED: {name}: {e}", file=sys.stderr)
            failed = True
    apply_edits()
    print("\n".join(done))
    sys.exit(1 if failed else 0)


def walk(folder="", prefix=""):
    """Yield (path, doc) for every document, descending into folders."""
    path = f"/documents/{folder}" if folder else "/documents/"
    for d in json.loads(fetch(path)):
        name = d["VissibleName"]
        if d["Type"] == "CollectionType":
            yield from walk(d["ID"], f"{prefix}{name}/")
        else:
            yield prefix + name, d


def stem(name):
    return name[:-4] if name.lower().endswith(".pdf") else name


def select(docs, wanted):
    if wanted == "--last":
        return [max(docs, key=lambda pd: pd[1]["ModifiedClient"])]
    by_id = [pd for pd in docs if pd[1]["ID"] == wanted]
    if by_id:
        return by_id
    w = stem(wanted).lower()
    exact = [pd for pd in docs if stem(pd[1]["VissibleName"]).lower() == w]
    return exact or [pd for pd in docs if w in pd[0].lower()]


def main(argv):
    if not argv or argv[0] not in ("ls", "get", "put"):
        sys.exit(__doc__)
    if argv[0] == "put":
        put(argv[1:])
    docs = sorted(walk(), key=lambda pd: pd[1]["ModifiedClient"], reverse=True)
    if argv[0] == "ls":
        # Tablet clock is unreliable, so the dates are only good for ordering.
        for path, d in docs:
            kind = d.get("fileType", "")
            print(f"{d['ModifiedClient'][:10]}  {kind:<8}  {path}  [{d['ID']}]")
        return

    args, out = argv[1:], "."
    if "-o" in args:
        i = args.index("-o")
        out = args[i + 1]
        del args[i:i + 2]
    palette = None
    if "--palette" in args:
        i = args.index("--palette")
        if i + 1 >= len(args):
            sys.exit("--palette needs 'projector' or a palette file/JSON (viewer: Copy palette)")
        src = args[i + 1]
        del args[i:i + 2]
        if src == "projector":
            table = palette_table(PROJECTOR)
        else:
            try:
                p = json.loads(src) if src.lstrip().startswith("{") else json.load(open(src))
            except (OSError, ValueError) as e:
                sys.exit(f"palette {src!r}: {e}")
            if not isinstance(p, dict) or not isinstance(p.get("inks"), dict):
                sys.exit(f"palette {src!r}: expected the viewer's Copy palette JSON ({{bg, inks}})")
            table = palette_table(p["inks"])
        if not table:
            sys.exit(f"palette {src!r}: no mappable ink (green/blue/yellow/red are the pens exports can recolor)")
        palette = table
    if not args:
        sys.exit("usage: ./run get [-o DIR] [--palette projector|FILE] NAME|ID|--last ...")
    os.makedirs(out, exist_ok=True)
    failed = False
    for wanted in args:
        hits = select(docs, wanted)
        if not hits:
            print(f"no document matches {wanted!r} (see ./run ls)", file=sys.stderr)
            failed = True
            continue
        names = [stem(d["VissibleName"]) for _, d in hits]
        for path, d in hits:
            base = stem(d["VissibleName"]).replace("/", "_")
            if names.count(stem(d["VissibleName"])) > 1:
                base += f"-{d['ID'][:8]}"
            dest = os.path.join(out, base + ".pdf")
            try:
                data = fetch(f"/download/{d['ID']}/pdf", timeout=180)
            except SystemExit as e:
                print(f"FAILED: {path}: {e}", file=sys.stderr)
                failed = True
                continue
            with open(dest, "wb") as f:
                f.write(data)
            note = ""
            if palette:
                try:
                    k = recolor_pdf(dest, palette)
                    note = f", ink remapped ({k} stroke ops)"
                except Exception as e:
                    note = f", ink remap FAILED ({e}); stock colors kept"
            print(f"{path} -> {dest} ({len(data) // 1024} KB{note})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
