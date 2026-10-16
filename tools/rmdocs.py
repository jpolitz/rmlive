"""List documents on the tablet and download them as PDFs (ink included),
via the USB web interface. Called by `./run ls` and `./run get`.

  rmdocs.py ls
  rmdocs.py get [-o DIR] (NAME | ID | --last) ...

NAME matches exactly (case-insensitive, ".pdf" optional) if anything does,
otherwise as a substring; every match is downloaded.
"""
import json
import os
import sys
import urllib.request

WEB = "http://10.11.99.1"


def fetch(path, timeout=10):
    req = urllib.request.Request(WEB + path, headers={"Referer": WEB + "/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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
    if not argv or argv[0] not in ("ls", "get"):
        sys.exit(__doc__)
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
            except Exception as e:
                print(f"FAILED: {path}: {e}", file=sys.stderr)
                failed = True
                continue
            with open(dest, "wb") as f:
                f.write(data)
            print(f"{path} -> {dest} ({len(data) // 1024} KB)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
