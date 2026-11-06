# Version history

Versions follow [Semantic Versioning](https://semver.org/): **minor** (0.x.0) for new features or behaviour changes,
**patch** (0.0.x) for fixes and small improvements. Below 1.0, settings and routes may still change between minor
versions. The running version is `VERSION` in `server.py` (`python3 server.py --version`).

---

## 0.1.0 (2026-11-05): first public release

Split out of a home dashboard, where it has been copying games to a real Aurora console.

**Added**
- Web page (phone and PC) listing the games in a folder on this computer, with their covers (from each game's own
  `nxeart` package), sizes and whether they are on the Xbox's drive: complete, partly copied or missing.
- Copies over Aurora's FTP with progress, speed and time left; a queue that survives restarts; Stop; finished
  files are skipped, so sending a partly copied game again finishes it.
- Finds the Xbox by itself: scans the LAN for Aurora's web UI, so a changing DHCP address doesn't matter.
- Several hard drives: each drive gets a small tag file (`/Hdd1/xbox-drive.json`) and its own saved game list, so
  the page knows what is on which drive ("Also on Drive 2").
- Now playing (game and cover, or the Aurora menu) and CPU / GPU / RAM / case temperatures, from Aurora's web API.
- Free space: the drive size (typed in once per drive) minus what the drive scan counted; games that don't fit are
  refused.
- ▶ Play: starts a game that is on the drive.
- Local network only (plus `allow_clients`), same-origin POSTs, strict Content-Security-Policy. Python 3 standard
  library only, no build step.
