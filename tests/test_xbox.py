"""xbox.py against an in-memory fake of Aurora's FTP server; no Xbox needed."""
import ftplib
import io
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import xbox  # noqa: E402


class FakeAurora:
    """Aurora's FtpDll as far as xbox.py uses it: files {path: bytes}, dirs {path}, no REST."""

    def __init__(self):
        self.files = {}
        self.dirs = {"/Hdd1"}
        self.stored = []
        self.cwd_path = "/"
        self.fail_after = None  # bytes; then the "connection" drops mid-file

    def __call__(self):
        return self  # used as the ftp_factory

    def connect(self, host, port, timeout=None):
        self.host = host

    def login(self, user, password):
        if (user, password) != ("xbox", "secret"):
            raise ftplib.error_perm("530 Login incorrect")

    def quit(self):
        pass

    close = quit

    def cwd(self, path):
        if path not in self.dirs:
            raise ftplib.error_perm("550 No such directory")
        self.cwd_path = path

    def mkd(self, path):
        if path in self.dirs:
            raise ftplib.error_perm("550 Cannot create directory")
        self.dirs.add(path)

    def retrlines(self, cmd, callback):
        assert cmd == "LIST"  # FtpDll ignores path arguments with spaces
        base = self.cwd_path.rstrip("/") + "/"
        for d in sorted(self.dirs):
            if d.startswith(base) and "/" not in d[len(base):]:
                callback("drwxrwxrwx   1 root root             0 Aug 09 2023 " + d[len(base):])
        for p, body in sorted(self.files.items()):
            if p.startswith(base) and "/" not in p[len(base):]:
                callback("-rwxrwxrwx   1 root root %13d Aug 09 2023 %s" % (len(body), p[len(base):]))

    def retrbinary(self, cmd, callback):
        path = cmd[5:]
        if path not in self.files:
            raise ftplib.error_perm("550 File not found")
        callback(self.files[path])

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        assert rest is None
        name = cmd[5:]
        path = name if name.startswith("/") else self.cwd_path.rstrip("/") + "/" + name
        data = b""
        while True:
            block = fp.read(blocksize)
            if not block:
                break
            if self.fail_after is not None and len(data) + len(block) > self.fail_after:
                self.files[path] = data
                raise ftplib.error_temp("426 Connection closed")
            data += block
            if callback:
                callback(block)
        self.files[path] = data
        self.stored.append(path)


def make_xex(tid):
    """A default.xex header with only the execution info optional header."""
    head = bytearray(0x200)
    head[:4] = b"XEX2"
    struct.pack_into(">I", head, 0x14, 1)
    struct.pack_into(">II", head, 0x18, 0x00040006, 0x100)
    struct.pack_into(">I", head, 0x100 + 12, tid)
    return bytes(head)


def make_stfs(files):
    """A PIRS package (one hash block per table) holding small root files, laid out like the real nxeart."""
    header = bytearray(0xA000)
    header[:4] = b"PIRS"
    struct.pack_into(">I", header, 0x340, 0x971A)    # header size -> data starts at 0xA000
    header[0x37B] = 1                                  # block separation
    struct.pack_into("<H", header, 0x37C, 1)          # file table: 1 block ...
    header[0x37E:0x381] = (0).to_bytes(3, "little")    # ... at block 0
    blocks = [bytearray(0x1000)]                       # block 0: the file table
    table = blocks[0]
    for i, (name, body) in enumerate(files.items()):
        start = len(blocks)
        n = (len(body) + 0xFFF) // 0x1000
        e = bytearray(0x40)
        e[:len(name)] = name.encode()
        e[0x28] = 0x40 | len(name)                     # consecutive blocks
        e[0x2F:0x32] = start.to_bytes(3, "little")
        struct.pack_into(">I", e, 0x34, len(body))
        table[i * 0x40:(i + 1) * 0x40] = e
        for k in range(n):
            blocks.append(bytearray(body[k * 0x1000:(k + 1) * 0x1000].ljust(0x1000, b"\0")))
    return bytes(header) + bytes(0x1000) + b"".join(bytes(b) for b in blocks)  # hash block, then data


class XboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.games = os.path.join(self.base, "games")
        os.makedirs(self.games)
        env = os.path.join(self.base, ".env")
        with open(env, "w") as f:
            f.write("XBOX_FTP_USER=xbox\nXBOX_FTP_PASS=secret\n")
        os.chmod(env, 0o600)
        self.ftp = FakeAurora()

    def tearDown(self):
        self.tmp.cleanup()

    def add_game(self, name, files):
        for rel, body in files.items():
            p = os.path.join(self.games, name, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(body)

    def copier(self, drive={"id": "drive1", "label": "Drive 1"}):
        cfg = dict(xbox.DEFAULT_XBOX_CONFIG, enabled=True, games_dir=self.games, subnet="192.168.1.0/30")
        c = xbox.XboxCopier(cfg, self.base, ftp_factory=self.ftp)
        c._discover = lambda: "10.0.0.9"
        c._is_aurora = lambda ip: True
        if drive:
            self.ftp.files[xbox.DRIVE_FILE] = json.dumps(drive).encode()
        return c


class FilesTest(XboxTestCase):
    def test_title_id_from_xex(self):
        self.add_game("Halo 4", {"xs_h4_rf/default.xex": make_xex(0x4D530919)})
        g = xbox.scan_game(os.path.join(self.games, "Halo 4"))
        self.assertEqual(g["title_id"], "4D530919")
        self.assertEqual(g["files"], 1)

    def test_title_id_from_games_on_demand_folder(self):
        self.add_game("JUST DANCE 2016", {"555308D3/00007000/JD16": b"x"})
        self.assertEqual(xbox.scan_game(os.path.join(self.games, "JUST DANCE 2016"))["title_id"], "555308D3")

    def test_not_a_xex(self):
        self.add_game("Broken", {"default.xex": b"MZ" + bytes(100)})
        self.assertIsNone(xbox.scan_game(os.path.join(self.games, "Broken"))["title_id"])

    def test_stfs_reads_files_across_blocks(self):
        jpg = b"\xff\xd8\xff" + bytes(range(256)) * 30  # 7.7 KB: two blocks
        path = os.path.join(self.base, "nxeart")
        with open(path, "wb") as f:
            f.write(make_stfs({"nxebg.jpg": b"\xff\xd8\xffbg", "nxeslot.jpg": jpg}))
        pkg = xbox.Stfs(path)
        self.assertEqual(set(pkg.files()), {"nxebg.jpg", "nxeslot.jpg"})
        self.assertEqual(pkg.read("nxeslot.jpg"), jpg)

    def test_stfs_block_offsets_skip_hash_tables(self):
        pkg = xbox.Stfs.__new__(xbox.Stfs)
        pkg.base, pkg.shift = 0xA000, 0
        self.assertEqual(pkg.offset(0), 0xB000)
        self.assertEqual(pkg.offset(0xA9), 0xB000 + 0xA9 * 0x1000)
        self.assertEqual(pkg.offset(0xAA), 0xA000 + 0xAD * 0x1000)  # past a second level-0 and the level-1 hash block

    def test_cover_art_is_extracted_once(self):
        jpg = b"\xff\xd8\xff" + b"slot"
        self.add_game("Halo 4", {"xs/default.xex": make_xex(0x4D530919), "xs/nxeart": make_stfs({"nxeslot.jpg": jpg})})
        c = self.copier()
        c._refresh_library()
        gid = xbox.game_id("Halo 4")
        path = c.art(gid)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), jpg)
        os.remove(os.path.join(self.games, "Halo 4", "xs", "nxeart"))
        self.assertEqual(c.art(gid), path)  # cached
        self.assertIsNone(c.art("000000000000"))

    def test_long_names_cannot_be_sent(self):
        self.add_game("Too Long", {"a" * 43: b"x"})
        c = self.copier()
        c._refresh_library()
        status, msg = c.send(xbox.game_id("Too Long"))
        self.assertEqual(status, 409)
        self.assertIn("too long", msg)


class CopyTest(XboxTestCase):
    def ready(self, c):
        c.seen = 1e18
        c.tick()  # library, console, drive, inventory
        return c

    def test_copy_skips_finished_files_and_resends_unfinished_ones(self):
        self.add_game("GRID 2", {"default.xex": make_xex(0x434D0844), "data/big.bin": b"b" * 200000,
                                 "data/done.bin": b"d" * 5000})
        self.ftp.dirs |= {"/Hdd1/GAMES", "/Hdd1/GAMES/GRID 2", "/Hdd1/GAMES/GRID 2/data"}
        self.ftp.files["/Hdd1/GAMES/GRID 2/data/done.bin"] = b"d" * 5000
        self.ftp.files["/Hdd1/GAMES/GRID 2/data/big.bin"] = b"b" * 100  # cut short last time
        c = self.ready(self.copier())
        gid = xbox.game_id("GRID 2")
        self.assertEqual(c.status()["games"][0]["state"], "partial")
        self.assertEqual(c.send(gid), (200, None))
        self.assertEqual(c.send(gid)[0], 409)  # already queued
        c.tick()
        self.assertEqual(sorted(self.ftp.stored), ["/Hdd1/GAMES/GRID 2/data/big.bin", "/Hdd1/GAMES/GRID 2/default.xex"])
        self.assertEqual(self.ftp.files["/Hdd1/GAMES/GRID 2/data/big.bin"], b"b" * 200000)
        st = c.status()
        self.assertEqual(st["games"][0]["state"], "complete")
        self.assertEqual(st["last"]["result"], "done")
        self.assertEqual(st["queue"], [])
        with open(os.path.join(self.base, "data/xbox/drive-drive1.json")) as f:
            self.assertEqual(json.load(f)["games"]["grid 2"]["files"], 3)

    def test_new_game_folder_is_created(self):
        self.add_game("Halo 4", {"xs_h4_rf/maps/a.map": b"m" * 10})
        c = self.ready(self.copier())
        c.send(xbox.game_id("Halo 4"))
        c.tick()
        self.assertIn("/Hdd1/GAMES/Halo 4/xs_h4_rf/maps", self.ftp.dirs)
        self.assertEqual(self.ftp.files["/Hdd1/GAMES/Halo 4/xs_h4_rf/maps/a.map"], b"m" * 10)

    def test_failed_copy_is_reported_and_leaves_the_queue(self):
        self.add_game("A", {"f.bin": b"a" * 300000})
        self.add_game("B", {"f.bin": b"b" * 10})
        c = self.ready(self.copier())
        c.send(xbox.game_id("A"))
        c.send(xbox.game_id("B"))
        self.ftp.fail_after = 100000
        c.tick()
        st = c.status()
        self.assertEqual(st["last"]["result"], "failed")
        self.assertIn("426", st["last"]["error"])
        self.assertEqual(st["queue"], [xbox.game_id("B")])
        self.assertEqual(next(g for g in st["games"] if g["name"] == "A")["state"], "partial")

    def test_cancel_stops_the_copy(self):
        self.add_game("A", {"f.bin": b"a" * 300000})
        c = self.ready(self.copier())
        gid = xbox.game_id("A")
        c.send(gid)
        orig = c._progress

        def progress(done, sent, fn, window):
            orig(done, sent, fn, window)
            if c.job["sent"] > 0:
                c.cancel(gid)
        c._progress = progress
        c.tick()
        self.assertEqual(c.status()["last"]["result"], "cancelled")

    def test_queue_survives_a_restart(self):
        self.add_game("A", {"f.bin": b"a"})
        self.add_game("B", {"f.bin": b"b"})
        c = self.copier()
        c._refresh_library()
        c.send(xbox.game_id("A"))
        c.send(xbox.game_id("B"))
        c.cancel(xbox.game_id("A"))
        again = self.copier()
        self.assertEqual(again.queue, [xbox.game_id("B")])
        again.seen = 1e18
        again.tick()  # library, console, drive, inventory, then the saved queue
        self.assertEqual(self.ftp.files["/Hdd1/GAMES/B/f.bin"], b"b")
        self.assertEqual(self.copier().queue, [])

    def test_cancel_removes_from_queue(self):
        self.add_game("A", {"f.bin": b"a"})
        c = self.copier()
        c._refresh_library()
        gid = xbox.game_id("A")
        c.send(gid)
        self.assertEqual(c.cancel(gid), (200, None))
        self.assertEqual(c.status()["queue"], [])
        self.assertEqual(c.cancel(gid)[0], 404)

    def test_unnamed_drive_gets_the_next_name(self):
        self.add_game("A", {"f.bin": b"a"})
        os.makedirs(os.path.join(self.base, "data/xbox"))
        with open(os.path.join(self.base, "data/xbox/drive-drive1.json"), "w") as f:
            json.dump({"label": "Drive 1", "games": {"a": {"size": 1, "files": 1}}}, f)
        c = self.ready(self.copier(drive=None))
        st = c.status()
        self.assertEqual(st["drive"], {"id": None, "label": None})
        self.assertEqual(st["games"][0]["on_drives"], ["Drive 1"])
        self.assertEqual(c.name_drive(), (200, None))
        c.tick()
        self.assertEqual(json.loads(self.ftp.files[xbox.DRIVE_FILE])["id"], "drive2")
        st = c.status()
        self.assertEqual(st["drive"], {"id": "drive2", "label": "Drive 2"})
        self.assertEqual(c.name_drive()[0], 409)

    def test_wrong_password_shows_ftp_error(self):
        with open(os.path.join(self.base, ".env"), "w") as f:
            f.write("XBOX_FTP_USER=xbox\nXBOX_FTP_PASS=nope\n")
        c = self.ready(self.copier())
        self.assertEqual(c.status()["console"]["error"], "ftp")
        self.assertFalse(c.status()["console"]["found"])


class NovaTest(XboxTestCase):
    """Now playing, Play, and the free space (Aurora's web API faked)."""

    def ready(self, capacity=None):
        drive = {"id": "drive1", "label": "Drive 1"}
        if capacity:
            drive["capacity_gb"] = capacity
        c = self.copier(drive=drive)
        c.seen = 1e18
        c.tick()
        return c

    def test_space_counts_everything_on_the_drive(self):
        self.add_game("A", {"f.bin": b"a" * 1000})
        self.ftp.dirs |= {"/Hdd1/GAMES", "/Hdd1/GAMES/A", "/Hdd1/Content"}
        self.ftp.files["/Hdd1/GAMES/A/f.bin"] = b"a" * 1000
        self.ftp.files["/Hdd1/Content/tu.bin"] = b"c" * 500
        c = self.ready()
        sp = c.status()["space"]
        self.assertEqual(sp["used"], 1000 + 500 + len(self.ftp.files[xbox.DRIVE_FILE]))
        self.assertIsNone(sp["free"])
        self.assertEqual(c.set_capacity(250), (200, None))
        self.assertEqual(json.loads(self.ftp.files[xbox.DRIVE_FILE])["capacity_gb"], 250)
        sp = c.status()["space"]
        self.assertEqual(sp["capacity"], 250 * 10 ** 9 - xbox.DATA_PARTITION_OFFSET)
        self.assertEqual(sp["free"], sp["capacity"] - sp["used"])
        self.assertEqual(c.set_capacity("big")[0], 400)

    def test_send_refuses_a_game_that_does_not_fit(self):
        self.add_game("Big", {"f.bin": b"b" * 10})
        c = self.ready(capacity=8)
        c.drives["drive1"]["other_bytes"] = int(8e9) - xbox.DATA_PARTITION_OFFSET - (1 << 30) - 5  # 5 bytes left
        status, msg = c.send(xbox.game_id("Big"))
        self.assertEqual(status, 409)
        self.assertIn("Not enough space", msg)
        c.drives["drive1"]["other_bytes"] = 0
        self.assertEqual(c.send(xbox.game_id("Big")), (200, None))

    def test_play_launches_a_complete_game(self):
        self.add_game("Halo 4", {"xs_h4_rf/default.xex": make_xex(0x4D530919)})
        calls = []
        c = self.ready()
        c.nova.call = lambda ip, path, form=None: calls.append((ip, path, form))
        gid = xbox.game_id("Halo 4")
        self.assertEqual(c.play(gid)[0], 409)  # not on the drive yet
        c.send(gid)
        c.tick()
        self.assertTrue(c.status()["games"][0]["can_play"])
        self.assertEqual(c.play(gid), (200, None))
        self.assertEqual(calls, [("10.0.0.9", "/title/launch", {
            "exec": "default.xex", "path": "\\Device\\Harddisk0\\Partition1\\GAMES\\Halo 4\\xs_h4_rf", "type": "0"})])

    def test_now_playing_names_the_game(self):
        self.add_game("Halo 4", {"xs/default.xex": make_xex(0x4D530919)})
        c = self.ready()
        answers = {"/title": {"titleid": "0x4D530919", "path": "\\Device\\Harddisk0\\Partition1\\GAMES\\Halo 4\\xs\\default.xex"},
                   "/temperature": {"celsius": True, "cpu": 48.4, "gpu": 45.5, "memory": 39.6, "case": 30.3}}
        c.nova.call = lambda ip, path, form=None: answers[path]
        c.read_now()
        now = c.status()["now"]
        self.assertFalse(now["in_menu"])
        self.assertEqual((now["name"], now["game_id"]), ("Halo 4", xbox.game_id("Halo 4")))
        self.assertEqual(now["temps"]["cpu"], 48.4)
        answers["/title"] = {"titleid": "0x00000000", "path": "\\Device\\Harddisk0\\Partition1\\AU0.5B\\Aurora.xex"}
        c.read_now()
        self.assertTrue(c.status()["now"]["in_menu"])
        answers["/title"] = {"titleid": "0x12345678", "path": "\\Device\\Harddisk0\\Partition1\\GAMES\\Other\\default.xex"}
        c.read_now()
        self.assertEqual(c.status()["now"]["name"], "Other")

    def test_now_playing_login_error(self):
        c = self.ready()

        def refuse(ip, path, form=None):
            raise PermissionError("login refused")
        c.nova.call = refuse
        c.read_now()
        self.assertEqual(c.status()["now"]["error"], "login")


class ConfigTest(unittest.TestCase):
    def test_defaults_need_a_games_dir(self):
        with self.assertRaises(ValueError):
            xbox.validate_xbox_config(dict(xbox.DEFAULT_XBOX_CONFIG))
        xbox.validate_xbox_config(dict(xbox.DEFAULT_XBOX_CONFIG, games_dir="/x"))

    def test_subnet_is_checked_when_given(self):
        cfg = dict(xbox.DEFAULT_XBOX_CONFIG, games_dir="/x")
        xbox.validate_xbox_config(cfg)  # "" = this computer's /24
        cfg["subnet"] = "10.0.0.0/8"
        with self.assertRaises(ValueError):  # too big to scan
            xbox.validate_xbox_config(cfg)
        cfg["subnet"] = "nonsense"
        with self.assertRaises(ValueError):
            xbox.validate_xbox_config(cfg)
        cfg["subnet"] = "192.168.1.0/24"
        xbox.validate_xbox_config(cfg)

    def test_drive_label_needs_a_number(self):
        with self.assertRaises(ValueError):
            xbox.validate_xbox_config(dict(xbox.DEFAULT_XBOX_CONFIG, games_dir="/x", drive_label="Drive"))

    def test_read_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as f:
                f.write("# comment\nXBOX_FTP_USER=xbox\nexport XBOX_FTP_PASS=\"a b\"\nBROKEN\n")
            self.assertEqual(xbox.read_env(p), {"XBOX_FTP_USER": "xbox", "XBOX_FTP_PASS": "a b"})
            self.assertEqual(xbox.read_env(os.path.join(d, "missing")), {})

    def test_dest_dir_must_be_on_the_hard_drive(self):
        with self.assertRaises(ValueError):
            xbox.validate_xbox_config(dict(xbox.DEFAULT_XBOX_CONFIG, dest_dir="/Usb0/GAMES"))


if __name__ == "__main__":
    unittest.main()
