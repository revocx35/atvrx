# Architecture

```
browser ──HTTP──► uvicorn/FastAPI (atvrx.web) ──► Session worker thread ──TCP──► SpyServer / rtl_tcp
   ▲  JPEG frames + state over /ws                    └──► IQ file in /recordings (read-only)
```

## Components
| Part | Role |
| --- | --- |
| `web.py` | Sign-in (login, logout, password change), middleware (session check, CSRF header, CSP/nosniff/no-referrer/DENY, `no-store` on API), REST API, `/ws` |
| `auth.py` | scrypt hashes, throttle (10/IP, 5/account, 100 total per 15 min), server-side sessions bound to the password hash |
| `session.py` | One job at a time (watch or scan) in a worker thread; start/stop serialised by `_control`; idle release |
| `dsp.py` | AM envelope, vertical/horizontal sync lock, field resampling |
| `sources.py` | SpyServer 2.0.1700 and rtl_tcp clients, 8-bit IQ files |

## Key decisions
- The web layer never touches the radio directly; it changes `Config` and (re)starts the worker.
- A second Watch/Scan request stops the running worker first, under one lock, so two workers can't fight over a
  single-client SpyServer.

## Threat model
| Threat | Mitigation | Gap |
| --- | --- | --- |
| Password guessing | scrypt, per-IP/account/global throttle, dummy hash | no 2FA |
| CSRF | `X-ATVRX` header + `Sec-Fetch-Site`, `SameSite=Strict` | — |
| XSS / framing | strict CSP, DENY | CSP `connect-src` echoes the Host header |
| Path traversal | recordings resolved inside the folder only | — |
| Spoofed client IP | proxy headers only from `ATVRX_TRUSTED_PROXIES` (default private ranges) | any LAN host is a trusted proxy by default |
| Eavesdropping | `Secure` cookie on HTTPS | plain HTTP unless behind a TLS proxy |
| Outbound connections | only authenticated users choose the radio host/port | — |
