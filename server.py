#!/usr/bin/env python3
"""DropBox - a tiny self-hosted file sharing server (stdlib only)."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads"
STATIC_DIR = ROOT / "static"
INDEX_FILE = UPLOAD_DIR / "index.json"
INBOX_DIR = Path(os.environ.get("DROPBOX_INBOX", str(ROOT / "inbox")))
INBOX_LABEL = os.environ.get("DROPBOX_INBOX_LABEL", str(INBOX_DIR))
SCAN_INTERVAL = float(os.environ.get("DROPBOX_SCAN_INTERVAL", "3"))

MAX_UPLOAD = int(os.environ.get("DROPBOX_MAX_BYTES", 20 * 1024 * 1024 * 1024))
PORT = int(os.environ.get("DROPBOX_PORT", 8080))
HOST = os.environ.get("DROPBOX_HOST", "0.0.0.0")
ACCESS_CODE = os.environ.get("DROPBOX_CODE", "").strip()

_lock = threading.Lock()


def load_index() -> dict:
    if not INDEX_FILE.exists():
        return {}
    try:
        return json.loads(INDEX_FILE.read_text())
    except (ValueError, OSError):
        return {}


def save_index(idx: dict) -> None:
    tmp = INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, indent=2, ensure_ascii=False))
    tmp.replace(INDEX_FILE)


PACK_FILE = UPLOAD_DIR / "pack.json"
DEFAULT_PACK = {"name": "Modpack", "version": "", "minecraft": "",
                "loader": "", "notes": "", "updated": 0.0}


def load_pack() -> dict:
    pack = dict(DEFAULT_PACK)
    if PACK_FILE.exists():
        try:
            saved = json.loads(PACK_FILE.read_text())
            if isinstance(saved, dict):
                pack.update({k: saved.get(k, v) for k, v in DEFAULT_PACK.items()})
        except (ValueError, OSError):
            pass
    return pack


def save_pack(pack: dict) -> None:
    tmp = PACK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(pack, indent=2, ensure_ascii=False))
    tmp.replace(PACK_FILE)


def infer_pack(idx: dict) -> tuple[str, str]:
    """Guess Minecraft version + loader from the shared filenames."""
    import re
    counts: dict[str, int] = {}
    loader = ""
    for meta in idx.values():
        name = meta.get("name", "")
        for m in re.findall(r"(?<!\d)(1\.\d{1,2}(?:\.\d{1,2})?)(?!\d)", name):
            counts[m] = counts.get(m, 0) + 1
        low = name.lower()
        if not loader:
            for kw, val in (("neoforge", "NeoForge"), ("fabric", "Fabric"),
                            ("forge", "Forge"), ("quilt", "Quilt")):
                if kw in low:
                    loader = val
                    break
    mc = max(counts, key=counts.get) if counts else ""
    return mc, loader


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def safe_name(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip() or "file"
    cleaned = "".join(c for c in name if c.isprintable() and c not in '/\\\0')
    return cleaned[:200] or "file"


_pending: dict[str, tuple[int, float]] = {}


def register_path(src: Path, name: str | None = None, label: str = "",
                  folder: str = "") -> str | None:
    """Move a file into the shared store and return its token. Any file type."""
    name = safe_name(name or src.name)
    token = secrets.token_urlsafe(8)
    dest = UPLOAD_DIR / f"{token}__{name}"
    try:
        src.replace(dest)
    except OSError:
        try:
            import shutil
            shutil.move(str(src), dest)
        except OSError:
            return None
    size = dest.stat().st_size
    with _lock:
        idx = load_index()
        idx[token] = {"name": name, "size": size, "uploaded": time.time(),
                      "path": dest.name, "label": safe_label(label),
                      "folder": safe_folder(folder)}
        save_index(idx)
    return token


def safe_label(label: str) -> str:
    label = (label or "").strip()
    cleaned = "".join(c for c in label if c.isprintable() and c not in '/\\\0')
    return cleaned[:40]


def safe_folder(folder: str) -> str:
    parts = []
    for seg in (folder or "").replace("\\", "/").split("/"):
        seg = "".join(c for c in seg.strip() if c.isprintable() and c not in '/\\\0')
        if seg and seg not in (".", ".."):
            parts.append(seg[:60])
    return "/".join(parts)[:300]


def safe_arc(arc: str, used: set) -> str:
    parts = [p for p in arc.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    arc = "/".join(parts) or "file"
    base, ext = os.path.splitext(arc)
    cand, i = arc, 2
    while cand in used:
        cand = f"{base} ({i}){ext}"
        i += 1
    used.add(cand)
    return cand


def _scan_entry(entry: Path, label: str, folder: str = "") -> None:
    if entry.name.startswith(".") or not entry.is_file():
        return
    try:
        st = entry.stat()
    except OSError:
        return
    key = str(entry)
    prev = _pending.get(key)
    if prev is None or prev[0] != st.st_size or prev[1] != st.st_mtime:
        _pending[key] = (st.st_size, st.st_mtime)
        return
    if time.time() - st.st_mtime < 1.0:
        return
    name = entry.name
    if register_path(entry, label=label, folder=folder) is not None:
        where = f" [{folder}]" if folder else (f" [{label}]" if label else "")
        print(f"  inbox: shared '{name}'{where}", flush=True)
    _pending.pop(key, None)


def inbox_watcher() -> None:
    """Like a Minecraft mods folder: drop ANY file (or a folder) in, it gets shared."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  inbox:   {INBOX_LABEL}", flush=True)
    while True:
        try:
            for entry in sorted(INBOX_DIR.iterdir()):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    folder = safe_folder(entry.name)
                    for child in sorted(entry.iterdir()):
                        _scan_entry(child, folder, folder)
                else:
                    _scan_entry(entry, "", "")
        except Exception as exc:
            print(f"  inbox watcher error: {exc}", flush=True)
        time.sleep(SCAN_INTERVAL)


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def check_code(handler) -> bool:
    if not ACCESS_CODE:
        return True
    supplied = handler.headers.get("X-Access-Code") or \
        urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query).get("code", [""])[0]
    return secrets.compare_digest(supplied, ACCESS_CODE)


class Handler(BaseHTTPRequestHandler):
    server_version = "DropBox/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _base_url(self):
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        if proto not in ("http", "https"):
            proto = "http"
        host = self.headers.get("Host") or f"{lan_ip()}:{PORT}"
        return f"{proto}://{host}"

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, msg, status=200):
        body = msg.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/files":
            if not check_code(self):
                return self._json({"error": "access code required"}, 401)
            return self._json(self._list_files())
        if path == "/api/pack":
            return self._json(self._pack_info())
        if path == "/api/stats":
            if not check_code(self):
                return self._json({"error": "access code required"}, 401)
            return self._json(self._stats())
        if path.startswith("/f/"):
            return self._serve_download(path[len("/f/"):], query)
        if path == "/zip":
            return self._serve_zip(query.get("folder", [""])[0])
        if path == "/api/health":
            return self._json({"ok": True, "name": "DropBox"})
        return self._text("Not found", 404)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/f/"):
            return self._serve_download(parsed.path[len("/f/"):], {}, head=True)
        return self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/upload":
            return self._upload()
        if parsed.path == "/api/pack":
            return self._set_pack()
        if parsed.path == "/api/delete":
            return self._delete()
        if parsed.path == "/api/label":
            return self._set_label()
        return self._text("Not found", 404)

    # ---------- static ----------
    def _serve_static(self, rel: str):
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._text("Not found", 404)
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------- api ----------
    def _list_files(self):
        idx = load_index()
        base = self._base_url()
        items = []
        labels = set()
        for token, meta in idx.items():
            label = meta.get("label", "")
            if label:
                labels.add(label)
            items.append({
                "token": token,
                "name": meta["name"],
                "size": meta["size"],
                "size_human": human(meta["size"]),
                "uploaded": meta["uploaded"],
                "label": label,
                "folder": meta.get("folder", ""),
                "url": f"{base}/f/{token}/{urllib.parse.quote(meta['name'])}",
            })
        items.sort(key=lambda x: x["uploaded"], reverse=True)
        return {"files": items, "count": len(items), "inbox": INBOX_LABEL,
                "labels": sorted(labels, key=str.lower)}

    def _upload(self):
        if not check_code(self):
            return self._json({"error": "access code required"}, 401)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._json({"error": "bad length"}, 400)
        if length < 0:
            return self._json({"error": "bad length"}, 400)
        if length > MAX_UPLOAD:
            return self._json({"error": f"file too large (max {human(MAX_UPLOAD)})"}, 413)

        raw_name = self.headers.get("X-Filename", "")
        try:
            raw_name = base64.b64decode(raw_name).decode("utf-8", "replace")
        except Exception:
            raw_name = urllib.parse.unquote(raw_name)
        name = safe_name(raw_name)

        raw_label = self.headers.get("X-Label", "")
        if raw_label:
            try:
                raw_label = base64.b64decode(raw_label).decode("utf-8", "replace")
            except Exception:
                raw_label = urllib.parse.unquote(raw_label)
        label = safe_label(raw_label)

        raw_folder = self.headers.get("X-Folder", "")
        if raw_folder:
            try:
                raw_folder = base64.b64decode(raw_folder).decode("utf-8", "replace")
            except Exception:
                raw_folder = urllib.parse.unquote(raw_folder)
        folder = safe_folder(raw_folder)

        token = secrets.token_urlsafe(8)
        dest = UPLOAD_DIR / f"{token}__{name}"
        remaining = length
        try:
            with dest.open("wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            return self._json({"error": str(exc)}, 500)

        if remaining > 0:
            dest.unlink(missing_ok=True)
            return self._json({"error": "incomplete upload"}, 400)

        with _lock:
            idx = load_index()
            idx[token] = {
                "name": name,
                "size": length,
                "uploaded": time.time(),
                "path": dest.name,
                "label": label,
                "folder": folder,
            }
            save_index(idx)

        base = self._base_url()
        return self._json({
            "token": token,
            "name": name,
            "size": length,
            "label": label,
            "folder": folder,
            "url": f"{base}/f/{token}/{urllib.parse.quote(name)}",
        })

    def _delete(self):
        if not check_code(self):
            return self._json({"error": "access code required"}, 401)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._json({"error": "bad request"}, 400)
        token = payload.get("token", "")
        with _lock:
            idx = load_index()
            meta = idx.pop(token, None)
            if meta:
                save_index(idx)
                (UPLOAD_DIR / meta["path"]).unlink(missing_ok=True)
        if not meta:
            return self._json({"error": "not found"}, 404)
        return self._json({"deleted": token})

    def _set_label(self):
        if not check_code(self):
            return self._json({"error": "access code required"}, 401)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._json({"error": "bad request"}, 400)
        token = payload.get("token", "")
        label = safe_label(payload.get("label", ""))
        with _lock:
            idx = load_index()
            meta = idx.get(token)
            if not meta:
                return self._json({"error": "not found"}, 404)
            meta["label"] = label
            save_index(idx)
        return self._json({"token": token, "label": label})

    def _serve_download(self, rest: str, query: dict, head: bool = False):
        token = urllib.parse.unquote(rest.split("/", 1)[0])
        force = query.get("dl", ["0"])[0] in ("1", "true", "yes")
        idx = load_index()
        meta = idx.get(token)
        if not meta:
            return self._text("File not found or expired", 404)
        target = UPLOAD_DIR / meta["path"]
        if not target.is_file():
            return self._text("File missing on disk", 410)

        size = target.stat().st_size
        disposition = "attachment" if force else "inline"
        ctype = mimetypes.guess_type(meta["name"])[0] or "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition",
                         f"{disposition}; filename*=UTF-8''{urllib.parse.quote(meta['name'])}")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if head:
            return
        with target.open("rb") as fh:
            while chunk := fh.read(1024 * 256):
                self.wfile.write(chunk)

    def _stats(self):
        idx = load_index()
        total = sum(m.get("size", 0) for m in idx.values())
        folders = sorted({m.get("folder", "") for m in idx.values() if m.get("folder")})
        bytype: dict[str, int] = {}
        for m in idx.values():
            ext = os.path.splitext(m.get("name", ""))[1].lower().lstrip(".") or "other"
            bytype[ext] = bytype.get(ext, 0) + m.get("size", 0)
        largest = sorted(idx.values(), key=lambda m: m.get("size", 0), reverse=True)[:6]
        try:
            du = shutil.disk_usage(str(UPLOAD_DIR))
            disk = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            disk = {"total": 0, "used": 0, "free": 0}
        return {
            "files": len(idx),
            "total": total,
            "folders": len(folders),
            "labels": sorted({m.get("label", "") for m in idx.values() if m.get("label")}),
            "bytype": sorted(({"ext": k, "size": v} for k, v in bytype.items()),
                             key=lambda x: x["size"], reverse=True)[:8],
            "largest": [{"name": m.get("name", ""), "size": m.get("size", 0),
                         "folder": m.get("folder", "")} for m in largest],
            "disk": disk,
        }

    def _pack_info(self):
        pack = load_pack()
        idx = load_index()
        mc, loader = infer_pack(idx)
        if not pack.get("minecraft"):
            pack["minecraft"] = mc
        if not pack.get("loader"):
            pack["loader"] = loader
        pack["detected"] = {"minecraft": mc, "loader": loader}
        pack["files"] = len(idx)
        base = self._base_url()
        pack["zip_url"] = f"{base}/zip"
        return pack

    def _set_pack(self):
        if not check_code(self):
            return self._json({"error": "access code required"}, 401)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._json({"error": "bad request"}, 400)
        pack = load_pack()

        def clean(v, n=120):
            return "".join(c for c in str(v) if c.isprintable())[:n].strip()

        for key in ("name", "version", "minecraft", "loader"):
            if key in payload:
                pack[key] = clean(payload[key], 60)
        if "notes" in payload:
            pack["notes"] = "".join(c for c in str(payload["notes"])
                                    if c.isprintable() or c == "\n")[:2000].strip()
        pack["updated"] = time.time()
        if not pack.get("name"):
            pack["name"] = "Modpack"
        with _lock:
            save_pack(pack)
        return self._json(self._pack_info())

    def _serve_zip(self, folder: str):
        import shutil
        import tempfile
        import zipfile

        folder = safe_folder(folder)
        prefix = folder + "/" if folder else ""
        idx = load_index()
        members = []
        for meta in idx.values():
            fo = meta.get("folder", "")
            if folder:
                if fo == folder:
                    arc = meta["name"]
                elif fo.startswith(prefix):
                    arc = fo[len(prefix):] + "/" + meta["name"]
                else:
                    continue
            else:
                arc = (fo + "/" + meta["name"]) if fo else meta["name"]
            src = UPLOAD_DIR / meta["path"]
            if src.is_file():
                members.append((src, arc))

        if not members:
            return self._text("Nothing to zip in this folder", 404)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=str(UPLOAD_DIR))
        used = set()
        try:
            pack = load_pack()
            mc, loader = infer_pack(idx)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for src, arc in members:
                    arc = safe_arc(arc, used)
                    zf.write(src, arc)
                info = {
                    "name": pack.get("name", "Modpack"),
                    "version": pack.get("version", ""),
                    "minecraft": pack.get("minecraft") or mc,
                    "loader": pack.get("loader") or loader,
                    "notes": pack.get("notes", ""),
                    "pack_updated": pack.get("updated", 0),
                    "generated": time.time(),
                    "files": len(members),
                }
                zf.writestr("modpack.json", json.dumps(info, indent=2, ensure_ascii=False))
            tmp.flush()
            size = os.path.getsize(tmp.name)
            if folder:
                name = (folder.rsplit("/", 1)[-1] or "folder") + ".zip"
            else:
                label = safe_name(f"{pack.get('name', 'modpack')}-v{pack.get('version')}"
                                  if pack.get("version") else pack.get("name", "modpack"))
                name = (label or "modpack") + ".zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(tmp.name, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, 1024 * 256)
        except (OSError, BrokenPipeError):
            pass
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def main():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    idx = load_index()
    dirty = False
    for token, meta in list(idx.items()):
        if not (UPLOAD_DIR / meta["path"]).exists():
            del idx[token]
            dirty = True
            continue
        if "folder" not in meta:
            meta["folder"] = safe_folder(meta.get("label", ""))
            dirty = True
    if dirty:
        save_index(idx)

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    ip = lan_ip()
    print("  DropBox — file share server")
    print(f"  local:   http://127.0.0.1:{PORT}")
    print(f"  network: http://{ip}:{PORT}")
    if ACCESS_CODE:
        print("  access code: required")
    print(f"  uploads: {UPLOAD_DIR}")
    threading.Thread(target=inbox_watcher, daemon=True).start()
    print("  Ctrl+C to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        httpd.shutdown()


if __name__ == "__main__":
    main()
