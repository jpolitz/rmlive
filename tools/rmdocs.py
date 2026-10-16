"""List documents on the tablet and download them as PDFs (ink included),
via the USB web interface. Called by `./run ls` and `./run get`.

  rmdocs.py ls
  rmdocs.py get [-o DIR] (NAME | ID | --last) ...
  rmdocs.py replace FILE.pdf

NAME matches exactly (case-insensitive, ".pdf" optional) if anything does,
otherwise as a substring; every match is downloaded.

replace uploads FILE and makes it the one document with that name, in the
old one's folder. If the old one has ink, the new PDF goes into the old
document instead (ink stays on its pages), which needs the same page count.
Both edit xochitl's files over ssh with xochitl stopped.
"""
import json
import os
import subprocess
import sys
import time

WEB = "http://10.11.99.1"
HOST = os.environ.get("RM_HOST", "remarkable")
DATA = "/home/root/.local/share/remarkable/xochitl"


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


def upload(path):
    """POST to /upload like the web page does. Returns the new document's ID."""
    before = {d["ID"] for _, d in walk()}
    name = os.path.basename(path)
    code = curl("-o", "/dev/null", "-w", "%{http_code}", "-H", f"Origin: {WEB}",
                "-F", f"file=@{path};type=application/pdf", WEB + "/upload", timeout=60)
    if code != b"201":
        sys.exit(f"upload failed ({code.decode()}): {name}")
    for _ in range(20):
        new = [d["ID"] for _, d in walk() if d["ID"] not in before]
        if new:
            return new[0]
        time.sleep(0.5)
    sys.exit(f"uploaded {name} but it never appeared in the document list")


def ssh(cmd):
    return subprocess.run(["ssh", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR", HOST, cmd],
                          check=True, capture_output=True, text=True).stdout


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


def restart_xochitl(edits):
    """Run shell `edits` in the data dir with xochitl stopped, then start it."""
    ssh(f"systemctl stop xochitl && cd {DATA} && {{ {edits} ; }}; systemctl start xochitl")
    for _ in range(60):
        if subprocess.run(["curl", "-sf", "-m", "2", "-o", "/dev/null", WEB + "/documents/"]).returncode == 0:
            return
        time.sleep(0.5)
    print("xochitl restarted but the USB web interface isn't answering yet;"
          " toggle it off/on if it stays down", file=sys.stderr)


def replace(path):
    name = os.path.basename(path)
    olds = [(p, d) for p, d in walk() if stem(d["VissibleName"]).lower() == stem(name).lower()]
    if not olds:
        upload(path)
        print(f"uploaded: {name} (nothing to replace)")
        return
    if len(olds) > 1:
        listing = "\n".join(f"  {p}  [{d['ID']}]" for p, d in olds)
        sys.exit(f"--replace: {len(olds)} documents named {stem(name)!r}, not sure which:\n{listing}")
    old_path, old = olds[0]
    oid = old["ID"]
    info = json.loads(ssh(f"cd {DATA} && printf '{{\"ink\": %s, \"meta\": ' "
                          f"$(ls {oid}/*.rm 2>/dev/null | wc -l) && cat {oid}.metadata && "
                          f"printf ', \"content\": ' && cat {oid}.content && printf '}}'"))
    parent = info["meta"]["parent"]
    if info["ink"] == 0:
        new = upload(path)
        restart_xochitl(f"rm -rf {oid} {oid}.* && "
                        f"sed -i 's/\"parent\": \"[^\"]*\"/\"parent\": \"{parent}\"/' {new}.metadata")
        print(f"replaced: {old_path}")
        return
    # Ink lives on page entries that point at original PDF pages, so swap the PDF
    # underneath them, which only makes sense if the pages line up.
    c = info["content"]  # formatVersion 2 has cPages; 1 (fresh uploads) has originalPageCount
    have = c["cPages"]["original"]["value"] if "cPages" in c else c.get("originalPageCount")
    want = pdf_pages(path)
    if want != have:
        sys.exit(f"--replace: {old_path} has ink and {have} pages, the new PDF has "
                 f"{want if want is not None else 'an unknown number of'} pages; refusing so the ink "
                 f"doesn't land on the wrong pages. Plain ./run put keeps both.")
    subprocess.run(["scp", "-q", "-o", "LogLevel=ERROR", path, f"{HOST}:{DATA}/{oid}.pdf.new"], check=True)
    now = int(time.time() * 1000)
    size = os.path.getsize(path)
    restart_xochitl(f"mv {oid}.pdf.new {oid}.pdf && rm -rf {oid}.thumbnails && "
                    f"sed -i 's/\"lastModified\": \"[0-9]*\"/\"lastModified\": \"{now}\"/' {oid}.metadata && "
                    f"sed -i 's/\"sizeInBytes\": \"[0-9]*\"/\"sizeInBytes\": \"{size}\"/' {oid}.content")
    print(f"replaced: {old_path} (kept ink on {info['ink']} page(s))")


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
    if not argv or argv[0] not in ("ls", "get", "replace"):
        sys.exit(__doc__)
    if argv[0] == "replace":
        for path in argv[1:]:
            replace(path)
        return
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
    if not args:
        sys.exit("usage: ./run get [-o DIR] NAME|ID|--last ...")
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
            print(f"{path} -> {dest} ({len(data) // 1024} KB)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
