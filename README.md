# Video Capture

A single-file PyQt6 application for capturing analogue video from V4L2 USB
capture cards. Designed for digitising VHS, Hi8, Betamax, and other analogue
sources on Linux with full hardware-accelerated encoding support.

![screenshot placeholder](docs/screenshot.png)

---

## Features

- **Live preview** during both standby and recording — no blind captures
- **VAAPI hardware encoding** — `hevc_vaapi`, `h264_vaapi`, `av1_vaapi` on Intel/AMD
- **Software encoding fallback** — `libsvtav1`, `libaom-av1`, `libx265`, `libx264`
- **PipeWire audio** capture with real-time monitoring through speakers
- **Video filters** — deinterlace (yadif), rotate, flip, zoom/crop, brightness,
  contrast, saturation, gamma
- **GStreamer preview path** for libcamera/IPU6 devices (PipeWire nodes)
- **All settings persisted** across sessions via QSettings
- **Codec auto-detection** — queries `ffmpeg -encoders` at startup, shows only
  what your build actually supports

---

## Requirements

### Runtime

| Package | Notes |
|---------|-------|
| `python3` | 3.10+ |
| `python3-pyqt6` | |
| `ffmpeg` | With `libv4l2` and `libpulse` — RPM Fusion build recommended |
| `gstreamer1-plugins-good` | For PipeWire/libcamera preview path |
| `gstreamer1-plugins-bad-free` | `pipewiresrc`, `fdsink` elements |
| `pipewire` + `pipewire-pulseaudio` | Audio capture and monitoring |
| `v4l-utils` | Device and format detection (`v4l2-ctl`) |

### Recommended for VAAPI encoding (Intel)

```bash
sudo dnf install intel-media-driver libva-utils
```

Verify VAAPI is working:

```bash
vainfo | grep -A1 HEVC
```

You should see `VAEntrypointEncSlice` listed under a HEVC profile. If you only
see `i965-va-driver` entries, switch to `intel-media-driver`:

```bash
sudo dnf swap i965-va-driver intel-media-driver
```

---

## Installation

### From COPR (recommended)

```bash
sudo dnf copr enable yourusername/video-capture
sudo dnf install video-capture
```

### From source

```bash
git clone https://github.com/yourusername/video-capture
cd video-capture
python3 video_capture.py
```

No build step required — the application is a single Python file.

---

## Quickstart

1. **Plug in your capture card.** USB capture cards based on the Macrosilicon
   MS2109 chipset (sold under many brand names) are well tested. The device
   will appear as `/dev/video0`.

2. **Launch the app.**

3. **Select your device** from the Device dropdown. Hit **↻ SCAN** if it does
   not appear.

4. **Set Input Format to MJPEG.** Raw YUYV overflows USB 2.0 bandwidth on most
   cards and causes corrupted frames. The app auto-selects MJPEG when available.

5. **Click ◉ PREVIEW** to verify the picture. The preview command is logged to
   the LOG tab — check there if nothing appears.

6. **Set your output file** in the CAPTURE tab. The default is
   `~/Videos/capture_YYYYMMDD_HHMMSS.webm`.

7. **Click ● REC** to start recording. Preview stays live during recording via
   a scaled-down tee from the encode pipeline. Click **■ STOP** when done.

---

## Configuration

### CAPTURE tab

| Setting | Notes |
|---------|-------|
| Device | V4L2 or PipeWire video node |
| Input Format | **MJPEG** recommended for USB cards; YUYV causes bandwidth errors |
| Resolution | 720×480 (NTSC) or 720×576 (PAL) for standard VHS |
| Framerate | 29.97 for NTSC, 25 for PAL |
| Deinterlace | `yadif` enabled by default — VHS is interlaced |
| Audio Device | PipeWire source; `.monitor` sources are filtered out |
| Monitor audio | Pass audio through speakers during preview and recording |

### CODEC tab

| Setting | Notes |
|---------|-------|
| Video Encoder | Auto-populated from `ffmpeg -encoders`; VAAPI encoders listed first |
| CQ / CRF | Quality target; 28 is a good default for HEVC/AVC |
| Speed Preset | Higher = faster encode, lower quality; 6 recommended for real-time |
| Audio Encoder | `libopus` recommended for WebM/MKV; `aac` for MP4 |

### VIDEO tab

Deinterlace, rotate, flip, zoom, and colour correction (brightness, contrast,
saturation, gamma). All filters apply to both preview and the encoded file.

### ADVANCED tab

| Setting | Notes |
|---------|-------|
| Thread Queue Size | Increase if you see "thread queue is blocking" in logs |
| V4L2 Buffers | Default 4; increase if frames are dropped at startup |
| A/V Sync Offset | Fine-tune audio/video alignment in seconds |
| Video Filter Chain | Manual `ffmpeg -vf` chain; overrides the deinterlace checkbox |
| Extra Global Opts | Appended before all inputs, e.g. `-loglevel verbose` |
| Reset All Settings | Clears saved settings and restores defaults |

---

## Troubleshooting

**Preview freezes the app**
: This was a known bug (fixed). If you see it, ensure you are running the
  latest version — older builds called `QThread.wait()` from daemon threads
  which deadlocked against Qt's signal delivery mutex.

**"No such input format: yuyv" / "Error opening input file"**
: Set Input Format to **MJPEG** in the CAPTURE tab.

**"Dequeued v4l2 buffer contains corrupted data"**
: Same cause — YUYV at full resolution overflows USB 2.0. Use MJPEG.

**No picture, preview command shows in LOG tab**
: Run the logged command directly in a terminal to see the raw ffmpeg error.

**VAAPI encode fails with "No device found"**
: Install `intel-media-driver` (Intel) or `mesa-va-drivers` (AMD).
  Check `vainfo` — you need `VAEntrypointEncSlice` for the chosen codec.

**`hevc_vaapi` fails with profile error**
: The encoder requires `-profile:v main` (8-bit, NV12 input). The app sets
  this automatically; if you are running the encode command manually, add it.

**Audio out of sync**
: Use the **A/V Sync Offset** slider in the ADVANCED tab. Positive values
  delay audio relative to video.

**Settings not saved between runs**
: Settings are stored in `~/.config/video-capture/video-capture.conf`.
  Use **Reset All Settings** in the ADVANCED tab to clear a corrupt config.

---

## Architecture notes

The application uses a strict threading model to keep the UI responsive:

- All ffmpeg and GStreamer processes run in `QThread` workers
- Workers communicate exclusively via Qt signals (`QueuedConnection`)
- `QThread.wait()` is never called — not from the main thread, not from daemon
  threads. SIGTERM + scheduled SIGKILL is used for shutdown instead
- During recording, preview is fed from a `rawvideo` tee output on the capture
  ffmpeg process's stdout rather than a separate device open

---

## Building the RPM / publishing to COPR

See [`build-srpm.sh`](build-srpm.sh). Prerequisites:

```bash
sudo dnf install rpm-build rpmdevtools copr-cli
copr-cli login
```

Then:

```bash
COPR_PROJECT=yourusername/video-capture ./build-srpm.sh 0.1.0
```

The script assembles the source tarball, builds the SRPM, and submits it to
COPR in one step. Make sure your COPR project has the **RPM Fusion Free**
external repository enabled so the `ffmpeg` dependency resolves at install time.

---

## License

MIT — see [LICENSE](LICENSE).
