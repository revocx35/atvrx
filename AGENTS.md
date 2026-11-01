# AGENTS.md

Notes for AI agents working on ATV-RX. Design and threat model: [ARCHITECTURE.md](ARCHITECTURE.md); user guide: [README.md](README.md).

## Layout
- `atvrx/web.py` FastAPI app: sign-in, session middleware, CSRF header rule, API, `/ws` live JPEG stream.
- `atvrx/auth.py` scrypt `UserStore` (`/data/users.json`), `LoginThrottle`, in-memory `Sessions`; `atvrx/users.py` account CLI.
- `atvrx/session.py` `Session`: one worker thread owns the radio (watch or scan); `atvrx/dsp.py` demodulator and field slicer;
  `atvrx/scan.py` channel plans; `atvrx/sources.py` SpyServer, rtl_tcp and file sources.
- `atvrx/static/` vanilla JS UI. `tests/` (synthetic transmitter `synth.py`, fake servers `fakes.py`).

## Commands
- `python -m pytest -q` (requirements-dev.txt; ~30 s, no radio needed) or `docker build --target test .` (heavy).
- `docker compose exec atvrx python -m atvrx.users add NAME`.

## Conventions
- Every route except the sign-in page, `/healthz` and its assets needs a session; writes need `X-ATVRX: 1` and a
  same-origin `Sec-Fetch-Site`; `/ws` checks Origin against Host.
- `Session._run()` / `stop()` hold `_control`: request handlers run in parallel threads, and only one worker may hold the radio.
- Recordings are only opened by name inside the recordings folder (`_recording`).

## Never do
- Bypass `_control` when starting a worker; open files outside the recordings folder; trust `X-Forwarded-For`
  beyond `ATVRX_TRUSTED_PROXIES`; commit `/data` contents or `.env`.

## Gotchas
- SpyServer serves one client at a time; the app releases the radio after `ATVRX_IDLE_RELEASE_S` without viewers.
- Sessions are in memory: a restart signs everyone out.
