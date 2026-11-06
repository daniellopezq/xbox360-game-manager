# Xbox 360 Game Manager

A small web page for copying games from a computer to an Xbox 360 running [Aurora](https://phoenix.xboxunity.net/),
from your phone or PC. It shows your game library with covers, what is already on the Xbox, the free space,
what is playing and the console's temperatures, and it copies games over Aurora's FTP server with a queue.

- **Your library, with covers.** Each game folder's own `nxeart` package holds the picture the Xbox dashboard
  shows; the page uses it, so no internet lookup is needed.
- **What's on the Xbox.** Complete, partly copied or missing, per game. Several hard drives are supported: each
  drive gets a small tag file and its own saved list ("Also on Drive 2").
- **Copying.** Progress, speed and time left; a queue that survives restarts; Stop. Aurora's FTP can't resume a
  file, so an unfinished file is sent again, but finished files are skipped: sending a partly copied game again
  finishes it.
- **Finds the Xbox by itself.** It scans the local network for Aurora's web UI, so the console's address can change.
- **Now playing, temperatures, free space, Play.** From Aurora's web API (the NOVA plugin).
- **Python 3 standard library only.** No packages, no build step, no database. Runs on old hardware.

It needs a modded console (RGH/JTAG) running Aurora. It has been used with Aurora 0.5b and NOVA plugin 0.7 on a
Corona board, dashboard 17559. Other versions may differ; please report what works.

## Setup

1. **On the Xbox (Aurora settings):** turn on the FTP server and the web server (NOVA, port 9999). Note both logins
   (defaults: `xboxftp` / `xboxftp` and `xboxhttp` / `xboxhttp`; changing them is a good idea).
2. **On the computer with the games** (Linux, macOS or Windows with Python 3.9+):

   ```sh
   git clone https://github.com/daniellopezq/xbox360-game-manager.git
   cd xbox360-game-manager
   cp config.example.json config.json   # set games_dir
   cp .env.example .env && chmod 600 .env   # the two Aurora logins
   python3 server.py --check
   python3 server.py
   ```

3. Open `http://<this computer>:8360/` on a phone or PC on the same network.

To keep it running, see `deploy/xbox-game-manager.service` (systemd user service).

### Game folders

`games_dir` holds one folder per game, copied as-is to `dest_dir` on the Xbox (default `/Hdd1/GAMES`, which should
be one of Aurora's scan paths). Extracted games (a folder with `default.xex`) get a cover, a title ID and a Play
button. Games on Demand folders (`<title id>/00007000/...`) are copied too, but have no Play button yet. File and
folder names must be at most 42 characters (the Xbox's FATX limit); the page tells you when one isn't.

### Drives and free space

The first time a drive is seen, the page offers **Name this drive**: it writes `/Hdd1/xbox-drive.json` (the next
`drive1`, `drive2`... labelled with `drive_label`). Reading what is on a drive takes a few minutes over FTP; it
happens for a new drive and when you tap **Check again**. After each copy only that game is read again.

Aurora can't report free space, so type the size printed on the drive (e.g. 500 GB) once: it is saved in the
drive's tag file. Free space is that size, minus the system partitions, minus everything on `/Hdd1`. A game that
doesn't fit (with the rest of the queue, keeping 1 GB spare) isn't queued.

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `games_dir` | (required) | Folder with one sub-folder per game |
| `dest_dir` | `/Hdd1/GAMES` | Where games go on the Xbox (must be under `/Hdd1`) |
| `subnet` | `""` | Network to search for the Xbox, e.g. `192.168.1.0/24`; empty = this computer's /24. At most 4096 addresses. |
| `port`, `bind` | `8360`, `0.0.0.0` | Where the page is served |
| `allow_clients` | `[]` | Extra client networks besides private/loopback ones, e.g. a VPN range `["100.64.0.0/10"]` |
| `drive_label` | `Drive %d` | Names given to new drives |
| `web_port`, `ftp_port` | `9999`, `21` | Aurora's web UI and FTP ports |
| `env_file`, `data_dir` | `.env`, `data/xbox` | Logins file; where drive lists, covers and the queue are kept |
| `timeout_seconds` | `20` | FTP timeout |

## Security

- The page and API answer only private/loopback/link-local clients and `allow_clients`. Don't expose the port to the
  internet: anyone who reaches it can copy to and start games on your Xbox.
- POSTs must be same-origin JSON (CSRF protection); pages are sent with a strict Content-Security-Policy.
- Logins stay in `.env`. Aurora's FTP and web API are plain HTTP/FTP on your LAN, as Aurora provides them.

## How it works

- `xbox.py`: finding the Xbox, the drive scan, the copy queue (one background thread, idle when nobody has the
  page open and nothing is queued), the cover extraction (a minimal reader for the Xbox 360 STFS package format),
  title IDs from `default.xex` headers, and the web API client.
- `server.py`: the HTTP server and the JSON API (`/api/status`, `/api/art/<id>.jpg`; POST `/api/send`, `/cancel`,
  `/play`, `/rescan`, `/name-drive`, `/drive-size`).
- `web/`: the page (plain ES modules and CSS).

Aurora's FTP server has a few quirks this works around: no resume (REST), `SIZE` only with absolute paths, `LIST`
ignoring path arguments that contain spaces, and replies falling out of step after an unknown command.

Tests: `python3 -m unittest discover -s tests` (an in-memory fake of Aurora's FTP; no Xbox needed).

## Related

- [ha-xbox360-aurora](https://github.com/hudsonbrendon/ha-xbox360-aurora): a Home Assistant integration for the
  same console (sensors, temperatures, reboot / shutdown, launch). This project does the game copying and drive
  bookkeeping; the two work side by side.

Not affiliated with Microsoft, Aurora/Phoenix or the NOVA developers. Copy only games you own.

## License

MIT, see [LICENSE](LICENSE). Version history: [VERSION.md](VERSION.md).
