"""Xbox 360 game manager core: copy games from a folder on this computer to an Xbox 360 running Aurora.

The Xbox runs Aurora, which has an FTP server (port 21) and a web UI ("NOVA webUI" on port 9999, with an API).
A console's DHCP address can change, so it is found by scanning the LAN (`subnet`) for that web UI.
Logins come from the .env file: XBOX_FTP_USER / XBOX_FTP_PASS (FTP) and XBOX_WEB_USER / XBOX_WEB_PASS (web API).

Drives: hard drives get swapped. Each one carries DRIVE_FILE ({"id", "label", "capacity_gb"}) at its root; what is
on each drive is remembered in <data_dir>/drive-<id>.json, so the page can say what is where. A drive without the
file is "unnamed" until the page names it (the next driveN, labelled with `drive_label`).

Aurora's FtpDll (as tested): no REST, so an unfinished file is sent again whole (finished files, same size on both
sides, are skipped); SIZE wants an absolute path; LIST ignores a path argument with spaces (CWD there, then a bare
LIST); NLST returns LIST lines; an unknown command leaves replies out of step (reconnect).

Cover art: most game folders have an "nxeart" STFS package; its nxeslot.jpg (420x320, the Xbox dashboard's slot
picture) is extracted once into <data_dir>/art/.

Now playing and Play: Aurora's web API (NOVA plugin) gives the running title and the temperatures, polled every
10 s while the page is open, and launches a game (POST /title/launch, as Aurora's own web UI does).
Free space: Aurora reports none (no FTP command, no API call), so it is the drive's capacity (capacity_gb, typed in
on the page once per drive) minus the system partitions minus everything on /Hdd1, counted by the drive scan.
Python 3 standard library only.
"""
import concurrent.futures
import ftplib
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("xboxmgr")

DRIVE_FILE = "/Hdd1/xbox-drive.json"
DRIVE_ID = re.compile(r"^drive[0-9]{1,3}$")
AURORA_MARK = b"NOVA webUI"
FATX_NAME_MAX = 42
# The data partition starts 0x130EB0000 bytes into a 360 hard drive (the rest is system partitions).
DATA_PARTITION_OFFSET = 0x130EB0000
HDD1_DEVICE = "\\Device\\Harddisk0\\Partition1"
MENU_TITLES = {"00000000", "FFFE07D1"}  # Aurora itself / the Xbox dashboard
SPACE_MARGIN = 1 << 30                  # keep 1 GB free
LIST_LINE = re.compile(r"^([d-])\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\w{3}\s+\d+\s+[\d:]+\s(.+)$")

DEFAULT_XBOX_CONFIG = {
    "enabled": True,
    "games_dir": "",              # one sub-folder per game, copied as-is
    "dest_dir": "/Hdd1/GAMES",    # on the Xbox (an Aurora scan path)
    "subnet": "",                 # scanned for Aurora's web UI, e.g. "192.168.1.0/24"; "" = this computer's /24
    "drive_label": "Drive %d",    # names given to new drives
    "web_port": 9999,
    "ftp_port": 21,
    "env_file": ".env",
    "data_dir": "data/xbox",
    "timeout_seconds": 20,
}


def validate_xbox_config(c):
    if not isinstance(c["enabled"], bool):
        raise ValueError("enabled must be true or false")
    if not isinstance(c["drive_label"], str) or c["drive_label"].count("%d") != 1 or len(c["drive_label"]) > 30:
        raise ValueError("drive_label must contain %d once, like \"Drive %d\"")
    for key in ("games_dir", "subnet", "env_file", "data_dir"):
        if not isinstance(c[key], str):
            raise ValueError("%s must be a string" % key)
    if not isinstance(c["dest_dir"], str) or not re.match(r"^/Hdd1(/[^/]+)+$", c["dest_dir"]):
        raise ValueError("dest_dir must be a folder under /Hdd1, like /Hdd1/GAMES")
    for key in ("web_port", "ftp_port"):
        if not isinstance(c[key], int) or isinstance(c[key], bool) or not 1 <= c[key] <= 65535:
            raise ValueError("%s must be a port number" % key)
    t = c["timeout_seconds"]
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 1 <= t <= 120:
        raise ValueError("timeout_seconds must be 1-120")
    if c["enabled"]:
        if not c["games_dir"] or not c["data_dir"] or not c["env_file"]:
            raise ValueError("games_dir, data_dir and env_file are needed when enabled")
        if c["subnet"]:
            try:
                net = ipaddress.ip_network(c["subnet"])
            except ValueError:
                raise ValueError("subnet must be a network like 192.168.1.0/24")
            if net.version != 4 or net.num_addresses > 4096:
                raise ValueError("subnet must be an IPv4 network of at most 4096 addresses")


def read_env(path):
    """{NAME: value} from a .env file (NAME=value lines; quotes around the value are removed). {} if missing."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read(65536).splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[name.strip()] = value
    return out


def local_subnet():
    """The /24 around this computer's LAN address (the one the default route uses)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET address: nothing is sent, it only picks the outgoing interface
        ip = s.getsockname()[0]
    finally:
        s.close()
    return str(ipaddress.ip_network(ip + "/24", strict=False))


def title_id(xex_path):
    """The 8-hex-digit title ID from a default.xex (XEX2 execution info header), or None."""
    try:
        with open(xex_path, "rb") as f:
            h = f.read(0x1000)
            if h[:4] != b"XEX2":
                return None
            count = struct.unpack(">I", h[0x14:0x18])[0]
            for i in range(min(count, 64)):
                key, value = struct.unpack(">II", h[0x18 + i * 8:0x20 + i * 8])
                if key == 0x00040006:
                    f.seek(value)
                    info = f.read(24)
                    return "%08X" % struct.unpack(">I", info[12:16])[0] if len(info) == 24 else None
    except (OSError, struct.error):
        pass
    return None


class Stfs:
    """Just enough of the Xbox 360 STFS package format (CON/LIVE/PIRS) to read small root files."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            h = f.read(0x3A0)
        if len(h) < 0x3A0 or h[:4] not in (b"PIRS", b"LIVE", b"CON "):
            raise ValueError("not an STFS package")
        self.base = (struct.unpack(">I", h[0x340:0x344])[0] + 0xFFF) & 0xFFFFF000
        self.shift = (~h[0x37B]) & 1  # block separation: 1 or 2 hash blocks per table
        self.table_blocks = struct.unpack("<H", h[0x37C:0x37E])[0]
        self.table_start = int.from_bytes(h[0x37E:0x381], "little")

    def offset(self, block):
        s = self.shift
        backing = (((block + 0xAA) // 0xAA) << s) + block
        if block >= 0xAA:
            backing += ((block + 0x70E4) // 0x70E4) << s
            if block >= 0x70E4:
                backing += 1 << s
        return (backing << 12) + self.base

    def files(self):
        """{name: (start block, size)} of the files in the package (directories left out)."""
        out = {}
        with open(self.path, "rb") as f:
            for i in range(min(self.table_blocks, 16)):
                f.seek(self.offset(self.table_start + i))
                b = f.read(0x1000)
                for j in range(0, len(b) - 0x3F, 0x40):
                    e = b[j:j + 0x40]
                    n = e[0x28] & 0x3F
                    if n and not e[0x28] & 0x80:
                        out[e[:n].decode("latin-1")] = (int.from_bytes(e[0x2F:0x32], "little"),
                                                         struct.unpack(">I", e[0x34:0x38])[0])
        return out

    def read(self, name, limit=4 << 20):
        start, size = self.files()[name]
        if size > limit:
            raise ValueError("file too large")
        data = bytearray()
        with open(self.path, "rb") as f:
            block = start
            while len(data) < size:
                f.seek(self.offset(block))
                data += f.read(0x1000)
                block += 1
        return bytes(data[:size])


def _find(root, names, depth=2):
    """First file under root (at most `depth` folders down) whose lower-case name is in `names`."""
    for dirpath, dirs, files in os.walk(root):
        for fn in sorted(files):
            if fn.lower() in names:
                return os.path.join(dirpath, fn)
        if dirpath[len(root):].count(os.sep) >= depth:
            dirs[:] = []
        dirs.sort()
    return None


def scan_game(path):
    """{"size", "files", "title_id", "art_pkg", "long_names"} for one game folder."""
    size = files = 0
    long_names = []
    for dirpath, dirs, fns in os.walk(path):
        for n in dirs + fns:
            if len(n) > FATX_NAME_MAX:
                long_names.append(n)
        for fn in fns:
            try:
                size += os.path.getsize(os.path.join(dirpath, fn))
                files += 1
            except OSError:
                pass
    xex = _find(path, {"default.xex"})
    tid = title_id(xex) if xex else None
    launch = None
    if tid:
        rel = os.path.relpath(os.path.dirname(xex), path)
        launch = {"dir": "" if rel == "." else rel.replace(os.sep, "\\"), "exec": os.path.basename(xex)}
    if not tid:  # Games on Demand: <title id>/00007000/...
        tid = next((d.upper() for d in sorted(os.listdir(path))
                    if re.match(r"^[0-9A-Fa-f]{8}$", d) and os.path.isdir(os.path.join(path, d, "00007000"))), None)
    return {"size": size, "files": files, "title_id": tid, "art_pkg": _find(path, {"nxeart"}),
            "long_names": long_names[:3], "launch": launch}


def game_id(name):
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


# --- the Xbox -------------------------------------------------------------------------------------

class Nova:
    """Aurora's web API (the NOVA plugin, port 9999): POST /authenticate (form username / password) gives a
    token for "Authorization: Bearer"; a 401 later means log in again (once)."""

    def __init__(self, env_path, port, timeout):
        self.env_path, self.port, self.timeout = env_path, port, timeout
        self.token = self.ip = None
        self.lock = threading.Lock()

    def _open(self, req):
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read(262144)
        return json.loads(body) if body.strip() else None

    def _login(self, ip):
        env = read_env(self.env_path)
        form = urllib.parse.urlencode({"username": env.get("XBOX_WEB_USER", ""),
                                       "password": env.get("XBOX_WEB_PASS", "")}).encode()
        data = self._open(urllib.request.Request("http://%s:%d/authenticate" % (ip, self.port), data=form))
        if not isinstance(data, dict) or not data.get("token"):
            raise PermissionError("no token")
        self.token, self.ip = data["token"], ip

    def call(self, ip, path, form=None):
        """JSON from GET path (or POST form). Raises OSError / ValueError / PermissionError."""
        with self.lock:
            for attempt in (0, 1):
                if not self.token or self.ip != ip:
                    self._login(ip)
                req = urllib.request.Request("http://%s:%d%s" % (ip, self.port, path),
                                             data=urllib.parse.urlencode(form).encode() if form else None,
                                             headers={"Authorization": "Bearer " + self.token})
                try:
                    return self._open(req)
                except urllib.error.HTTPError as exc:
                    if exc.code != 401:
                        raise
                    self.token = None
                    if attempt:
                        raise PermissionError("login refused")


class Cancelled(Exception):
    pass


class XboxCopier:
    def __init__(self, cfg, base_dir, ftp_factory=ftplib.FTP):
        self.cfg = cfg
        self.enabled = cfg["enabled"]
        self.games_dir = cfg["games_dir"]
        self.data_dir = os.path.join(base_dir, cfg["data_dir"])
        self.env_path = os.path.join(base_dir, cfg["env_file"])
        self.ftp_factory = ftp_factory
        self.nova = Nova(self.env_path, cfg["web_port"], min(10, cfg["timeout_seconds"]))
        self.now = None                  # what the Xbox is running, from the web API
        self.now_thread = None
        self.now_due = 0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.seen = 0.0                  # last time the page asked; the worker idles without a viewer
        self.library = {}                # id -> game
        self.library_mtime = None
        self.console = {"found": False, "ip": None, "checked_ms": None, "error": None}
        self.drive = None                # {"id", "label"} of the drive in the Xbox now, or {"id": None} if unnamed
        self.drives = self._load_drives()
        self.queue = self._load_queue()  # saved, so a restart picks up where it was (the current copy first)
        self.job = None
        self.last = None                 # result of the last copy
        self.cancel_id = None
        self.want_rescan = False
        self.want_name = False

    # --- page API ---
    def status(self):
        self.seen = time.time()
        self.wake.set()
        with self.lock:
            drive_id = (self.drive or {}).get("id")
            rec = self.drives.get(drive_id, {}) if drive_id else {}
            inv = rec.get("games", {})
            games = []
            for gid, g in sorted(self.library.items(), key=lambda kv: kv[1]["name"].lower()):
                there = inv.get(g["name"].lower())
                games.append({
                    "id": gid, "name": g["name"], "size": g["size"], "title_id": g["title_id"],
                    "art": bool(g["art_pkg"]), "state": _state(g, there),
                    "on_xbox_size": there["size"] if there else 0,
                    "on_drives": sorted(d["label"] for k, d in self.drives.items()
                                        if k != drive_id and _state(g, d["games"].get(g["name"].lower())) == "complete"),
                    "problem": ("Name too long for the Xbox: %s" % g["long_names"][0]) if g["long_names"] else None,
                    "can_play": bool(g["launch"]) and _state(g, there) == "complete",
                })
            return {
                "enabled": self.enabled,
                "console": dict(self.console),
                "drive": dict(self.drive) if self.drive else None,
                "drive_scanned_ms": rec.get("scanned_ms"),
                "space": self._space(rec),
                "now": dict(self.now) if self.now and self.console["found"] else None,
                "scanning": self.want_rescan,
                "library_ready": self.library_mtime is not None,
                "games": games,
                "queue": list(self.queue),
                "job": dict(self.job) if self.job else None,
                "last": dict(self.last) if self.last else None,
            }

    def send(self, gid):
        with self.lock:
            g = self.library.get(gid)
            if not g:
                return 404, "No such game."
            if g["long_names"]:
                return 409, "A file name is too long for the Xbox (42 characters): %s" % g["long_names"][0]
            if gid in self.queue or (self.job and self.job["id"] == gid):
                return 409, "Already queued."
            rec = self.drives.get((self.drive or {}).get("id"), {})
            space = self._space(rec)
            if space and space["free"] is not None:
                inv = rec.get("games", {})
                pending = sum(self._missing(self.library[q], inv) for q in self.queue + ([self.job["id"]] if self.job else [])
                              if q in self.library)
                need = self._missing(g, inv)
                if need + pending > space["free"] - SPACE_MARGIN:
                    return 409, "Not enough space on %s: needs %.1f GB, about %.1f GB free%s." % (
                        self.drive["label"], need / 1e9, max(0, space["free"] - pending) / 1e9,
                        " after the queue" if pending else "")
            self.queue.append(gid)
            self._save_queue()
        self.wake.set()
        return 200, None

    def cancel(self, gid):
        with self.lock:
            if self.job and self.job["id"] == gid:
                self.cancel_id = gid
            elif gid in self.queue:
                self.queue.remove(gid)
                self._save_queue()
            else:
                return 404, "Not queued."
        return 200, None

    def rescan(self):
        with self.lock:
            self.want_rescan = True
        self.wake.set()

    def name_drive(self):
        """Ask the worker to write DRIVE_FILE on an unnamed drive. 409 if the drive already has a name."""
        with self.lock:
            if not self.drive or self.drive.get("id"):
                return 409, "The drive in the Xbox already has a name." if self.drive else "The Xbox isn't connected."
            self.want_name = True
        self.wake.set()
        return 200, None

    def play(self, gid):
        """Start a game on the Xbox (it must be complete on the drive in it now)."""
        with self.lock:
            g = self.library.get(gid)
            ip = self.console["ip"] if self.console["found"] else None
            inv = self.drives.get((self.drive or {}).get("id"), {}).get("games", {})
        if not g:
            return 404, "No such game."
        if not ip:
            return 409, "The Xbox isn't connected."
        if not g["launch"] or _state(g, inv.get(g["name"].lower())) != "complete":
            return 409, "%s isn't on this drive (or can't be started from here)." % g["name"]
        # The device path, as Aurora's own launcher sends it (/plugin path.launcher).
        folder = "\\".join([HDD1_DEVICE] + self.cfg["dest_dir"].split("/")[2:] + [g["name"]])
        if g["launch"]["dir"]:
            folder += "\\" + g["launch"]["dir"]
        try:
            self.nova.call(ip, "/title/launch", {"exec": g["launch"]["exec"], "path": folder, "type": "0"})
        except (OSError, ValueError, PermissionError) as exc:
            log.warning("xbox: launching %s failed (%s)", g["name"], type(exc).__name__)
            return 502, "The Xbox didn't start it (%s)." % type(exc).__name__
        log.info("xbox: started %s", g["name"])
        with self.lock:
            self.now_due = 0
        return 200, None

    def set_capacity(self, gb):
        """Remember the size of the drive in the Xbox (in its DRIVE_FILE), for the free space."""
        if not isinstance(gb, (int, float)) or isinstance(gb, bool) or not 8 <= gb <= 20000:
            return 400, "Give the drive size in GB (8-20000), as printed on it."
        with self.lock:
            drive = dict(self.drive or {})
        if not drive.get("id") or not self.console["found"]:
            return 409, "Name the drive first." if self.console["found"] else "The Xbox isn't connected."
        drive["capacity_gb"] = gb
        body = json.dumps(dict(drive, created=time.strftime("%Y-%m-%d")), indent=1).encode("utf-8")
        try:
            f = self._ftp()
            try:
                f.storbinary("STOR " + DRIVE_FILE, io.BytesIO(body))
            finally:
                _quit(f)
        except (OSError, ftplib.Error, EOFError) as exc:
            return 502, _ftp_message(exc)
        with self.lock:
            self.drive = drive
        return 200, None

    def _space(self, rec):
        """{"capacity", "used", "free"} in bytes (capacity and free None without capacity_gb), or None before a scan."""
        if "other_bytes" not in rec:
            return None
        used = rec["other_bytes"] + sum(g["size"] for g in rec["games"].values())
        gb = (self.drive or {}).get("capacity_gb")
        cap = int(gb * 1e9) - DATA_PARTITION_OFFSET if gb else None
        return {"capacity": cap, "used": used, "free": max(0, cap - used) if cap else None}

    @staticmethod
    def _missing(g, inv):
        there = inv.get(g["name"].lower())
        return max(0, g["size"] - (there["size"] if there else 0))

    def art(self, gid):
        """Path of the cover picture (extracted on first use), or None."""
        with self.lock:
            g = self.library.get(gid)
        if not g or not g["art_pkg"]:
            return None
        path = os.path.join(self.data_dir, "art", gid + ".jpg")
        if os.path.isfile(path):
            return path
        try:
            data = Stfs(g["art_pkg"]).read("nxeslot.jpg")
        except (OSError, KeyError, ValueError, struct.error):
            return None
        if data[:3] != b"\xff\xd8\xff":
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path

    # --- worker ---
    def start(self):
        if self.enabled and not self.thread:
            self.thread = threading.Thread(target=self._run, name="xbox", daemon=True)
            self.thread.start()
            self.now_thread = threading.Thread(target=self._run_now, name="xbox-now", daemon=True)
            self.now_thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def _run(self):
        while not self.stopping.is_set():
            self.wake.wait(5)
            self.wake.clear()
            if self.stopping.is_set():
                break
            if time.time() - self.seen > 90 and not self.queue:
                continue  # nobody is looking and nothing to copy
            try:
                self.tick()
            except Exception:
                log.exception("xbox: worker step failed")

    def _run_now(self):
        """Separate from the worker, so "now playing" stays fresh during a long copy."""
        while not self.stopping.wait(1):
            if time.time() < self.now_due or time.time() - self.seen > 90 or not self.console["found"]:
                continue
            self.now_due = time.time() + 10
            try:
                self.read_now()
            except Exception:
                log.exception("xbox: now playing failed")

    def read_now(self):
        ip = self.console["ip"]
        try:
            title = self.nova.call(ip, "/title") or {}
            temps = self.nova.call(ip, "/temperature") or {}
        except PermissionError:
            with self.lock:
                self.now = {"error": "login", "checked_ms": int(time.time() * 1000)}
            return
        except (OSError, ValueError):
            with self.lock:
                self.now = {"error": "unreachable", "checked_ms": int(time.time() * 1000)}
            return
        tid = str(title.get("titleid") or "0").upper().replace("0X", "").zfill(8)
        path = str(title.get("path") or "")
        in_menu = tid in MENU_TITLES or path.lower().endswith("aurora.xex")
        with self.lock:
            gid, name = None, None
            if not in_menu:
                gid = next((k for k, g in self.library.items() if g["title_id"] == tid), None)
                name = self.library[gid]["name"] if gid else _name_from_path(path) or tid
            self.now = {"in_menu": in_menu, "title_id": tid, "game_id": gid, "name": name,
                        "temps": {k: temps[k] for k in ("cpu", "gpu", "memory", "case")
                                  if isinstance(temps.get(k), (int, float))},
                        "checked_ms": int(time.time() * 1000), "error": None}

    def tick(self):
        self._refresh_library()
        if time.time() * 1000 - (self.console["checked_ms"] or 0) > 30000:  # also when it wasn't found
            self._check_console()
        if not self.console["found"]:
            return
        if self.want_name:
            self._name_drive()
        drive_id = (self.drive or {}).get("id")
        if drive_id and (self.want_rescan or drive_id not in self.drives):
            self._inventory()
        if self.queue and drive_id:
            self._copy(self.queue[0])

    def _refresh_library(self):
        try:
            mtime = os.stat(self.games_dir).st_mtime_ns
        except OSError:
            return
        if mtime == self.library_mtime:
            return
        lib = {}
        for name in sorted(os.listdir(self.games_dir)):
            path = os.path.join(self.games_dir, name)
            if os.path.isdir(path) and not name.startswith("."):
                g = scan_game(path)
                g.update(name=name, path=path)
                lib[game_id(name)] = g
        with self.lock:
            self.library, self.library_mtime = lib, mtime
        log.info("xbox: %d games in %s", len(lib), self.games_dir)

    def _check_console(self):
        ip = self.console["ip"]
        if not (ip and self._is_aurora(ip)):
            ip = self._discover()
        drive = self._read_drive(ip) if ip else None
        with self.lock:
            if ip != self.console["ip"]:
                log.info("xbox: %s", "found at %s" % ip if ip else "not found")
            self.console.update(found=bool(ip), ip=ip, checked_ms=int(time.time() * 1000),
                                error=None if ip else "not_found")
            if ip and drive is None:
                self.console.update(found=False, error="ftp")
            self.drive = drive

    def _is_aurora(self, ip):
        try:
            with socket.create_connection((ip, self.cfg["web_port"]), timeout=0.5):
                pass
            with urllib.request.urlopen("http://%s:%d/" % (ip, self.cfg["web_port"]), timeout=3) as r:
                return AURORA_MARK in r.read(4096)
        except (OSError, ValueError):
            return False

    def _discover(self):
        def probe(ip):
            s = socket.socket()
            s.settimeout(0.4)
            try:
                if s.connect_ex((ip, self.cfg["web_port"])) != 0:
                    return None
            finally:
                s.close()
            return ip if self._is_aurora(ip) else None
        hosts = [str(a) for a in ipaddress.ip_network(self.cfg["subnet"] or local_subnet()).hosts()]
        with concurrent.futures.ThreadPoolExecutor(128) as ex:
            return next((ip for ip in ex.map(probe, hosts) if ip), None)


    def _ftp(self, ip=None):
        env = read_env(self.env_path)
        f = self.ftp_factory()
        f.connect(ip or self.console["ip"], self.cfg["ftp_port"], timeout=self.cfg["timeout_seconds"])
        f.login(env.get("XBOX_FTP_USER", ""), env.get("XBOX_FTP_PASS", ""))
        return f

    def _read_drive(self, ip):
        """{"id", "label"} from DRIVE_FILE, {"id": None} without it, None if FTP failed."""
        try:
            f = self._ftp(ip)
        except (OSError, ftplib.Error) as exc:
            log.warning("xbox: ftp login failed (%s)", type(exc).__name__)
            return None
        try:
            buf = io.BytesIO()
            try:
                f.retrbinary("RETR " + DRIVE_FILE, lambda b: buf.write(b) if buf.tell() < 4096 else None)
            except ftplib.error_perm:
                return {"id": None, "label": None}
            try:
                d = json.loads(buf.getvalue().decode("utf-8"))
            except ValueError:
                d = None
            if not isinstance(d, dict) or not DRIVE_ID.match(str(d.get("id"))):
                return {"id": None, "label": None}
            out = {"id": d["id"], "label": str(d.get("label") or d["id"])[:40]}
            gb = d.get("capacity_gb")
            if isinstance(gb, (int, float)) and not isinstance(gb, bool) and 8 <= gb <= 20000:
                out["capacity_gb"] = gb
            return out
        except (OSError, ftplib.Error):
            return None
        finally:
            _quit(f)

    def _name_drive(self):
        with self.lock:
            self.want_name = False
            if not self.drive or self.drive.get("id"):
                return
            n = 1 + max([int(k[5:]) for k in self.drives] + [0])
        drive = {"id": "drive%d" % n, "label": self.cfg["drive_label"] % n}
        body = json.dumps(dict(drive, created=time.strftime("%Y-%m-%d")), indent=1).encode("utf-8")
        f = self._ftp()
        try:
            f.storbinary("STOR " + DRIVE_FILE, io.BytesIO(body))
        finally:
            _quit(f)
        log.info("xbox: named the drive %s", drive["id"])
        with self.lock:
            self.drive = drive
            self.want_rescan = True

    # --- what is on the drive ---
    def _inventory(self, only=None):
        """Sizes of the game folders in dest_dir (all, or just `only`), saved per drive."""
        drive = dict(self.drive)
        dest = self.cfg["dest_dir"]
        f = self._ftp()
        try:
            try:
                names = [n for is_dir, _, n in _list(f, dest) if is_dir]
            except ftplib.error_perm:
                names = []  # a fresh drive: no dest_dir yet (the first copy makes it)
            if only is not None:
                names = [n for n in names if n.lower() == only.lower()]
            found = {n.lower(): _remote_size(f, dest + "/" + n) for n in names}
            other = None
            if only is None:  # everything else on the drive, for the free space
                top = dest.split("/")[2]
                other = sum((_remote_size(f, "/Hdd1/" + n)["size"] if is_dir else size)
                            for is_dir, size, n in _list(f, "/Hdd1") if n != top)
        finally:
            _quit(f)
        with self.lock:
            rec = self.drives.setdefault(drive["id"], {"label": drive["label"], "games": {}})
            rec["label"] = drive["label"]
            if only is None:
                rec["games"] = found
                rec["other_bytes"] = other
                rec["scanned_ms"] = int(time.time() * 1000)
                self.want_rescan = False
            else:
                rec["games"].pop(only.lower(), None)
                rec["games"].update(found)
            self._save_drive(drive["id"], rec)

    def _load_drives(self):
        out = {}
        try:
            names = os.listdir(self.data_dir)
        except OSError:
            return out
        for fn in names:
            m = re.match(r"^drive-(drive[0-9]{1,3})\.json$", fn)
            if not m:
                continue
            try:
                with open(os.path.join(self.data_dir, fn), encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec.get("games"), dict):
                    out[m.group(1)] = rec
            except (OSError, ValueError, AttributeError):
                log.warning("xbox: ignoring unreadable %s", fn)
        return out

    def _load_queue(self):
        try:
            with open(os.path.join(self.data_dir, "queue.json"), encoding="utf-8") as f:
                q = json.load(f).get("queue")
        except (OSError, ValueError, AttributeError):
            return []
        return [g for g in q if isinstance(g, str) and re.match(r"^[0-9a-f]{12}$", g)] if isinstance(q, list) else []

    def _save_queue(self):
        """Called with self.lock held."""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, "queue.json")
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"queue": self.queue}, f)
            os.replace(path + ".tmp", path)
        except OSError as exc:
            log.warning("xbox: could not save the queue (%s)", type(exc).__name__)

    def _save_drive(self, drive_id, rec):
        os.makedirs(self.data_dir, exist_ok=True)
        path = os.path.join(self.data_dir, "drive-%s.json" % drive_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, sort_keys=True)
        os.replace(tmp, path)

    # --- copying ---
    def _copy(self, gid):
        with self.lock:
            g = self.library.get(gid)
            if not g:
                self.queue.remove(gid)
                self._save_queue()
                return
            self.job = {"id": gid, "name": g["name"], "total": g["size"], "done": 0, "sent": 0,
                        "file": None, "speed": None, "started_ms": int(time.time() * 1000)}
            self.cancel_id = None
        log.info("xbox: copying %s (%.1f GB)", g["name"], g["size"] / 1e9)
        result, error = "done", None
        try:
            self._copy_tree(g)
        except Cancelled:
            result = "cancelled"
        except (OSError, ftplib.Error, EOFError) as exc:
            result, error = "failed", _ftp_message(exc)
            log.warning("xbox: copying %s failed: %s", g["name"], error)
        try:
            self._inventory(only=g["name"])
        except (OSError, ftplib.Error, EOFError) as exc:
            log.warning("xbox: could not re-read %s on the drive (%s)", g["name"], type(exc).__name__)
        with self.lock:
            job, self.job = self.job, None
            if gid in self.queue:
                self.queue.remove(gid)
                self._save_queue()
            if result == "failed":  # look for the Xbox again before the next game in the queue
                self.console["checked_ms"] = 0
            secs = max(1, (time.time() * 1000 - job["started_ms"]) / 1000)
            self.last = {"id": gid, "name": g["name"], "result": result, "error": error,
                         "seconds": int(secs), "sent": job["sent"], "ended_ms": int(time.time() * 1000)}
        log.info("xbox: %s %s (%d MB sent in %d s)", g["name"], result, job["sent"] // 1000000, secs)
        self.wake.set()

    def _copy_tree(self, g):
        dest = self.cfg["dest_dir"] + "/" + g["name"]
        f = self._ftp()
        window = []  # (time, bytes sent) for the speed
        try:
            _makedirs(f, dest)
            for dirpath, dirs, files in os.walk(g["path"]):
                dirs.sort()
                rel = os.path.relpath(dirpath, g["path"])
                rdir = dest if rel == "." else dest + "/" + rel.replace(os.sep, "/")
                if rel != ".":
                    _mkdir(f, rdir)
                there = {n.lower(): size for is_dir, size, n in _list(f, rdir) if not is_dir}
                for fn in sorted(files):
                    local = os.path.join(dirpath, fn)
                    size = os.path.getsize(local)
                    if there.get(fn.lower()) == size:
                        self._progress(size, 0, None, window)
                        continue
                    self._progress(0, 0, fn, window)
                    f.cwd(rdir)
                    with open(local, "rb") as src:
                        f.storbinary("STOR " + fn, src, 65536,
                                     callback=lambda block: self._progress(len(block), len(block), fn, window))
        finally:
            _quit(f)

    def _progress(self, done, sent, fn, window):
        now = time.time()
        with self.lock:
            if self.cancel_id == self.job["id"]:
                raise Cancelled()
            self.job["done"] += done
            self.job["sent"] += sent
            if fn:
                self.job["file"] = fn
            if sent:
                window.append((now, self.job["sent"]))
                while window and now - window[0][0] > 10:
                    window.pop(0)
                if len(window) > 1 and now - window[0][0] >= 1:
                    self.job["speed"] = int((window[-1][1] - window[0][1]) / (now - window[0][0]))


def _state(g, there):
    if not there or not there["size"]:
        return "missing"
    return "complete" if there["size"] >= g["size"] else "partial"


def _name_from_path(path):
    """\\Device\\Harddisk0\\Partition1\\GAMES\\Halo 4\\xs\\default.xex -> "Halo 4" (the folder under GAMES, else the xex's)."""
    parts = [p for p in path.split("\\") if p]
    for i, p in enumerate(parts[:-1]):
        if p.lower() == "games" and i + 1 < len(parts) - 1:
            return parts[i + 1]
    return parts[-2] if len(parts) >= 2 else None


def _quit(f):
    try:
        f.quit()
    except (OSError, ftplib.Error, EOFError):
        f.close()


def _list(f, path):
    """[(is_dir, size, name)] of one folder. FtpDll ignores LIST arguments with spaces, so CWD first."""
    f.cwd(path)
    lines = []
    f.retrlines("LIST", lines.append)
    return [(m.group(1) == "d", int(m.group(2)), m.group(3)) for m in map(LIST_LINE.match, lines) if m]


def _remote_size(f, path):
    """{"size", "files"} of a folder tree on the Xbox."""
    size = files = 0
    for is_dir, n, name in _list(f, path):
        if is_dir:
            sub = _remote_size(f, path + "/" + name)
            size += sub["size"]
            files += sub["files"]
        else:
            size += n
            files += 1
    return {"size": size, "files": files}


def _mkdir(f, path):
    try:
        f.mkd(path)
    except ftplib.error_perm:
        pass  # 550: it exists (or can't be made, and the CWD after it says so)


def _makedirs(f, path):
    parts = path.strip("/").split("/")
    for i in range(2, len(parts) + 1):  # /Hdd1 itself always exists
        _mkdir(f, "/" + "/".join(parts[:i]))


def _ftp_message(exc):
    text = str(exc).strip()
    if isinstance(exc, ftplib.Error) and text[:3].isdigit():
        return "The Xbox refused: %s" % text[:120]
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "The Xbox stopped answering."
    return "Connection to the Xbox failed (%s)." % type(exc).__name__
