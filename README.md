# ATV-RX

Recovers the picture from analog TV signals (PAL, SECAM, NTSC) received with an RTL-SDR or another SDR, and shows it live in the browser.

It connects to the radio over the network, so the SDR can live on another machine:

- **SpyServer** (Airspy SPY Server, protocol 2.0.1700)
- **rtl_tcp**
- **Recordings**: unsigned 8-bit IQ files as written by `rtl_sdr`

The picture is black and white and somewhat soft. An RTL-SDR captures about 2.4 MHz at once. That covers the vision carrier and the lower 2 MHz of the video, but not the colour subcarrier (4.43 MHz up in PAL) or the fine detail above 2 MHz.

## Run it

Copy `docker-compose.yml` and `.env.example` (as `.env`), put your SpyServer's address in `.env`, then:

```sh
docker compose up -d          # pulls ghcr.io/revocx35/atvrx:latest (amd64 and arm64)
```

Open `http://<host>:8095`. Use `docker compose up -d --build` to build from source instead. The address can also be changed on the page.

| Variable | Default | Meaning |
|---|---|---|
| `ATVRX_SOURCE` | `spyserver` | `spyserver`, `rtltcp` or `file` |
| `ATVRX_HOST` / `ATVRX_PORT_SDR` | `127.0.0.1` / `5555` | Where the radio server is |
| `ATVRX_FREQ_MHZ` | `487.25` | Vision carrier selected when the page opens |
| `ATVRX_GAIN` | `29` | Gain step (0 to the radio's maximum) |
| `ATVRX_STANDARD` | `625` | `625` (PAL/SECAM) or `525` (NTSC) |
| `ATVRX_IDLE_RELEASE_S` | `30` | Hand the radio back after this long with no browser open |
| `ATVRX_HTTP_PORT` | `8095` | Web port inside the container |

The app holds the radio only while someone is watching or a scan is running. SpyServer serves one client at a time, so close the page (or press **Stop**) before connecting SDR# or SDR++.

## Using it

- **Find channels** steps through the European UHF (E21-E69) or VHF (E5-E12) channel plan, or a frequency range. It lists carriers that carry the 15.6 kHz (or 15.7 kHz) line structure of analog video, along with the detected standard. Press **Watch** on a row to tune it.
- **Vision carrier** is the frequency of the picture carrier, 1.25 MHz above the channel's lower edge in Europe. Carrier drift is followed automatically.
- **Average fields** blends consecutive fields. It cuts the snow on weak signals, at the cost of smearing motion.
- **Line lock smoothing** sets how many lines the horizontal sync is averaged over. Raise it for weak signals, lower it for sources with jittery timing.
- **Positive modulation** is for French System L, where sync pulses are the weakest part of the signal instead of the strongest.

### Recordings

Put files in `./recordings`. They are mounted read-only into the container. Record one with:

```sh
rtl_sdr -f 488150000 -s 2400000 -g 49.6 -n 24000000 ch23.u8   # 10 s, vision carrier 0.9 MHz below centre
```

Choose **Recording** as the source, then enter the sample rate (2.4 MS/s) and the centre frequency (488.15 MHz).

## How it works

1. Tune so the vision carrier sits 0.9 MHz below the centre of the 2.4 MHz capture. The video is vestigial-sideband and lies almost entirely above the carrier.
2. Mix the carrier to 0 Hz, keep -0.25 to +2.0 MHz around it, and take the AM envelope. With negative modulation (System B/G/I/M), the sync tips are the strongest carrier and white is the weakest.
3. Find each field's vertical sync: the stretch where the line-averaged envelope stays near sync-tip level for 2.5-3 lines. Each field is locked on its own, with a flywheel and a full-field search when the timing jumps.
4. Lock each line to its own horizontal sync pulse, smoothed over a few lines, then resample the 52 µs visible part of every line.
5. Map blanking level to black and the brightest content to white, and stream the result to the browser as JPEG over a WebSocket.

## Tests

```sh
docker build --target test .
```

The suite runs against synthetic transmissions: sync pulses, a test picture, AM, noise, timing jumps, both standards and both polarities. It also covers fake SpyServer and rtl_tcp servers, which follow retune commands and mimic SpyServer's gain handling. Nothing in it needs a radio.
