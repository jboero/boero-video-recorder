#!/usr/bin/env python3
"""
Video Capture — V4L2 recorder with live preview, ffmpeg codec control.
Requires: PyQt6, ffmpeg (av1_vaapi or libaom-av1/libsvtav1, libopus)

Threading model
───────────────
  PreviewWorker   — QThread: runs ffmpeg -f rawvideo pipe, reads stdout in
                    fixed-size chunks (w*h*3 bytes), emits frame_ready(QImage).
                    Stderr is drained in a parallel daemon thread so it never
                    blocks the pipe read.

  CaptureWorker   — QThread: runs the encoding ffmpeg process, reads stderr
                    line-by-line via select() with a short timeout so the
                    thread stays interruptible, emits log_line / time_tick /
                    finished signals.  Never touches any Qt widget.

  All UI mutations happen exclusively on the main thread via Qt signals.
"""

import sys, os, subprocess, select, threading, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton,
    QGroupBox, QGridLayout, QLineEdit, QFileDialog, QSizePolicy,
    QFrame, QCheckBox, QTextEdit, QTabWidget, QMessageBox, QSplitter,
    QSlider,
)
from PyQt6.QtCore  import Qt, QThread, pyqtSignal, QTimer, QRect, QSettings
from PyQt6.QtGui   import (QFont, QPixmap, QImage, QPainter, QColor,
                           QPen, QKeySequence, QShortcut)

# ══════════════════════════════════════════════════════════════
#  Palette
# ══════════════════════════════════════════════════════════════

DARK_BG    = "#0e0e0f"
PANEL_BG   = "#17181a"
BORDER     = "#2a2b2e"
ACCENT     = "#e8a020"
ACCENT2    = "#4fa3e0"
TEXT_PRI   = "#e8e6e0"
TEXT_SEC   = "#7a7870"
REC_RED    = "#d94040"

QSS = f"""
QMainWindow, QWidget {{
    background:{DARK_BG}; color:{TEXT_PRI};
    font-family:"IBM Plex Mono","Courier New",monospace; font-size:12px;
}}
QGroupBox {{
    background:{PANEL_BG}; border:1px solid {BORDER}; border-radius:4px;
    margin-top:18px; padding:10px 8px 8px 8px;
    font-size:11px; color:{TEXT_SEC}; letter-spacing:1.5px;
}}
QGroupBox::title {{
    subcontrol-origin:margin; subcontrol-position:top left;
    padding:0 6px; left:10px; color:{ACCENT};
}}
QLabel {{ color:{TEXT_SEC}; font-size:11px; }}
QComboBox,QSpinBox,QDoubleSpinBox,QLineEdit {{
    background:{DARK_BG}; border:1px solid {BORDER}; border-radius:3px;
    color:{TEXT_PRI}; padding:4px 8px;
    font-family:"IBM Plex Mono",monospace; font-size:12px;
}}
QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus,QLineEdit:focus {{
    border:1px solid {ACCENT};
}}
QComboBox::drop-down {{ border:none; width:20px; }}
QComboBox::down-arrow {{
    width:10px; height:10px;
    border-left:4px solid transparent; border-right:4px solid transparent;
    border-top:6px solid {ACCENT}; margin-right:4px;
}}
QComboBox QAbstractItemView {{
    background:{PANEL_BG}; border:1px solid {BORDER};
    selection-background-color:{ACCENT}; selection-color:{DARK_BG};
    color:{TEXT_PRI};
}}
QPushButton {{
    background:{PANEL_BG}; border:1px solid {BORDER}; border-radius:3px;
    color:{TEXT_PRI}; padding:6px 16px;
    font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:0.5px;
}}
QPushButton:hover {{ border-color:{ACCENT}; color:{ACCENT}; }}
QPushButton:pressed {{ background:{ACCENT}; color:{DARK_BG}; }}
QPushButton#record {{
    background:{REC_RED}; color:#fff; border:none;
    font-size:13px; font-weight:bold; letter-spacing:2px; padding:10px 24px;
}}
QPushButton#record:hover {{ background:#ff5555; }}
QPushButton#stop {{
    background:#2a2a2a; color:{TEXT_SEC}; border:1px solid #444;
    font-size:13px; font-weight:bold; letter-spacing:2px; padding:10px 24px;
}}
QPushButton#stop:hover {{ border-color:{REC_RED}; color:{REC_RED}; }}
QCheckBox {{ color:{TEXT_PRI}; spacing:6px; }}
QCheckBox::indicator {{
    width:14px; height:14px; background:{DARK_BG};
    border:1px solid {BORDER}; border-radius:2px;
}}
QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT}; }}
QTabWidget::pane {{ border:1px solid {BORDER}; background:{PANEL_BG}; }}
QTabBar::tab {{
    background:{DARK_BG}; border:1px solid {BORDER}; border-bottom:none;
    padding:6px 16px; color:{TEXT_SEC}; font-size:11px; letter-spacing:1px;
}}
QTabBar::tab:selected {{
    background:{PANEL_BG}; color:{ACCENT}; border-top:2px solid {ACCENT};
}}
QTextEdit {{
    background:#0a0a0b; border:1px solid {BORDER};
    color:#7aff7a; font-family:"IBM Plex Mono","Courier New",monospace;
    font-size:11px; padding:6px;
}}
QStatusBar {{
    background:{PANEL_BG}; border-top:1px solid {BORDER};
    color:{TEXT_SEC}; font-size:11px;
}}
"""

# ══════════════════════════════════════════════════════════════
#  Device / encoder probing
# ══════════════════════════════════════════════════════════════

# Pixel formats ffmpeg's v4l2 demuxer can consume.
_CAPTURE_FORMATS = {
    "YUYV", "UYVY", "YUV4", "NV12", "NV21", "YU12", "YV12",
    "MJPG", "JPEG", "H264", "HEVC", "VP8",  "VP9",
    "RGB3", "BGR3", "RGBP", "BGRP",
}

# ── Device descriptor ─────────────────────────────────────────
# backend: "v4l2"      → ffmpeg -f v4l2 -i <path>  (/dev/videoN)
#          "libcamera" → requires PipeWire compat; we warn the user
# formats: pixel format list for v4l2 nodes; empty for libcamera
# ─────────────────────────────────────────────────────────────

def _pw_dump_nodes():
    """
    Parse pw-dump JSON and return only PipeWire:Interface:Node entries
    with media.class == "Video/Source".

    Returns list of dicts: {"id": int, "props": {...}}
    object.id is a top-level field on the entry, NOT inside props.
    """
    try:
        r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=6)
        data = json.loads(r.stdout)
    except Exception:
        return []

    nodes = []
    for entry in data:
        if entry.get("type") != "PipeWire:Interface:Node":
            continue
        props = entry.get("info", {}).get("props", {})
        if props.get("media.class") != "Video/Source":
            continue
        nodes.append({"id": entry.get("id"), "props": props})
    return nodes


def _v4l2_formats_for(dev_path):
    """Quick synchronous format query for a known-good v4l2 node."""
    fmts = []
    try:
        r = subprocess.run(
            ["v4l2-ctl", "--device", dev_path, "--list-formats"],
            capture_output=True, text=True, timeout=3
        )
        for line in r.stdout.splitlines():
            if "'" in line:
                tok = line.split("'")[1].strip().upper()
                if tok in _CAPTURE_FORMATS and tok not in fmts:
                    fmts.append(tok)
    except Exception:
        pass
    return fmts or ["YUYV"]


# ══════════════════════════════════════════════════════════════
#  DeviceScanWorker  — runs pw-dump off the main thread
# ══════════════════════════════════════════════════════════════

class DeviceScanWorker(QThread):
    """
    Calls pw-dump once, extracts Video/Source nodes, resolves formats
    for v4l2-backed nodes in a thread pool, emits scan_done.

    Result items: {
        "label":      str,          # shown in combo box
        "backend":    "v4l2" | "pipewire",
        "path":       str,          # /dev/videoN  (v4l2) or "" (pipewire)
        "pw_node_id": int | None,   # PipeWire object.id for pipewiresrc
        "formats":    [str, ...],   # pixel formats; [] for pipewire/libcamera
    }
    """
    scan_done     = pyqtSignal(list)
    scan_progress = pyqtSignal(str)

    def run(self):
        self.scan_progress.emit("Querying PipeWire…")
        raw_nodes = _pw_dump_nodes()

        if not raw_nodes:
            self.scan_done.emit([])
            return

        results = []

        v4l2_nodes   = [n for n in raw_nodes if n["props"].get("device.api") == "v4l2"]
        libcam_nodes = [n for n in raw_nodes if n["props"].get("device.api") == "libcamera"]

        # v4l2 nodes: resolve formats in parallel
        self.scan_progress.emit(
            f"Resolving formats for {len(v4l2_nodes)} V4L2 source(s)…")
        v4l2_formats = {}
        if v4l2_nodes:
            with ThreadPoolExecutor(max_workers=8) as pool:
                fut_map = {
                    pool.submit(_v4l2_formats_for, n["props"]["api.v4l2.path"]): n
                    for n in v4l2_nodes
                    if "api.v4l2.path" in n["props"]
                }
                for fut in as_completed(fut_map):
                    n = fut_map[fut]
                    v4l2_formats[n["props"]["api.v4l2.path"]] = fut.result()

        for n in v4l2_nodes:
            props = n["props"]
            path  = props.get("api.v4l2.path", "")
            desc  = props.get("node.description") or props.get("node.nick") or path
            results.append({
                "label":      f"{desc}  [{path}]",
                "backend":    "v4l2",
                "path":       path,
                "pw_node_id": n["id"],
                "formats":    v4l2_formats.get(path, ["YUYV"]),
            })

        # libcamera nodes — accessible via pipewiresrc using the PW node id
        for n in libcam_nodes:
            props = n["props"]
            desc  = (props.get("node.description") or
                     props.get("device.description") or
                     props.get("node.nick") or "libcamera device")
            results.append({
                "label":      f"{desc}  [PipeWire/libcamera]",
                "backend":    "pipewire",
                "path":       "",
                "pw_node_id": n["id"],
                "formats":    [],
            })

        # v4l2 first (direct ffmpeg support), pipewire second
        results.sort(key=lambda d: (0 if d["backend"] == "v4l2" else 1,
                                    d["label"]))
        self.scan_done.emit(results)


def list_v4l2_formats(dev_path):
    """Synchronous fallback."""
    return _v4l2_formats_for(dev_path)

# ─────────────────────────────────────────────────────────────────────────────
# Encoder + device probing — ALL run inside ProbeWorker (off the main thread).
# Nothing here may be called on the main thread.
# ─────────────────────────────────────────────────────────────────────────────

def _ffmpeg_encoders_raw():
    """Return raw stdout of 'ffmpeg -encoders -hide_banner', or ''."""
    try:
        r = subprocess.run(["ffmpeg", "-encoders", "-hide_banner"],
                           capture_output=True, text=True, timeout=8)
        return r.stdout
    except Exception:
        return ""

def _parse_video_encoders(raw: str) -> list[tuple[str,str]]:
    """
    Parse all video (V) encoders from 'ffmpeg -encoders' output.
    Returns list of (name, description) tuples, sorted with preferred
    hardware encoders first.

    Preferred order (if present):
      av1_vaapi, av1_nvenc, av1_qsv, hevc_vaapi, hevc_nvenc,
      vp9_vaapi, vp8_vaapi, libsvtav1, libaom-av1, libvpx-vp9,
      libvpx (VP8), libx265, libx264 — then everything else alpha.
    """
    PREF = [
        "av1_vaapi","av1_nvenc","av1_qsv","av1_amf",
        "hevc_vaapi","hevc_nvenc","hevc_qsv","hevc_amf",
        "vp9_vaapi","vp8_vaapi",
        "libsvtav1","libaom-av1","libvpx-vp9","libvpx",
        "libx265","libx264",
    ]
    found = {}   # name → desc
    for line in raw.splitlines():
        line = line.strip()
        # Lines look like:  " V..... libx264   ..."
        if len(line) > 8 and line[0] == "V":
            parts = line.split(None, 2)
            if len(parts) >= 2:
                name = parts[1]
                desc = parts[2].strip() if len(parts) > 2 else name
                found[name] = desc
    # Sort: preferred first in order, then rest alphabetically
    result = []
    for p in PREF:
        if p in found:
            result.append((p, found.pop(p)))
    for name in sorted(found):
        result.append((name, found[name]))
    return result

def _parse_audio_encoders(raw: str) -> list[tuple[str,str]]:
    """
    Parse all audio (A) encoders.  Preferred: libopus, aac, flac, libvorbis.
    """
    PREF = ["libopus","aac","flac","libvorbis","libmp3lame","pcm_s16le","pcm_s24le"]
    found = {}
    for line in raw.splitlines():
        line = line.strip()
        if len(line) > 8 and line[0] == "A":
            parts = line.split(None, 2)
            if len(parts) >= 2:
                name = parts[1]
                desc = parts[2].strip() if len(parts) > 2 else name
                found[name] = desc
    result = []
    for p in PREF:
        if p in found:
            result.append((p, found.pop(p)))
    for name in sorted(found):
        result.append((name, found[name]))
    return result

def _list_pw_audio_sources() -> list[tuple[str, str]]:
    """
    List PipeWire audio capture sources via pactl (available on all
    PipeWire systems via pipewire-pulse).  Returns [(pw_name, label), ...].
    The first entry is always "default" so the user can leave it unset.
    """
    devs: list[tuple[str, str]] = [("default", "Default (PipeWire)")]
    try:
        r = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=4)
        for line in r.stdout.splitlines():
            # columns: index  name  module  format  state
            parts = line.split()
            if len(parts) >= 2:
                name = parts[1]
                # skip monitor sources (they echo output back)
                if ".monitor" not in name:
                    devs.append((name, name))
    except Exception:
        pass
    return devs


# ══════════════════════════════════════════════════════════════
#  ProbeWorker  — probes ffmpeg encoders + ALSA devices off-thread
# ══════════════════════════════════════════════════════════════

class ProbeWorker(QThread):
    """
    Runs all subprocess probing (ffmpeg -encoders, pactl list sources) off
    the main thread so the UI never blocks.  Emits probe_done with:
      {
        "video_encoders": [(name, desc), ...],
        "audio_encoders": [(name, desc), ...],
        "pw_audio":       [(pw_name, label), ...],
      }
    """
    probe_done = pyqtSignal(dict)

    def run(self):
        raw = _ffmpeg_encoders_raw()
        result = {
            "video_encoders": _parse_video_encoders(raw),
            "audio_encoders": _parse_audio_encoders(raw),
            "pw_audio":       _list_pw_audio_sources(),
        }
        self.probe_done.emit(result)

# ══════════════════════════════════════════════════════════════
#  AudioMonitorWorker  — plays ALSA input through PipeWire/PulseAudio
# ══════════════════════════════════════════════════════════════

class AudioMonitorWorker(QThread):
    """
    Monitors audio from a PipeWire source to the default PipeWire sink:
        ffmpeg -n -hide_banner -f pipewire -i <source> -f pipewire -

    Used during preview so the operator can hear the tape audio.
    During recording the capture ffmpeg handles monitoring via its own
    second output, so this worker is stopped then.

    Same non-blocking shutdown contract as the other workers.
    """
    error = pyqtSignal(str)

    def __init__(self, pw_source: str):
        super().__init__()
        self.pw_source = pw_source
        self._proc     = None
        self._running  = True

    def run(self):
        cmd = [
            "ffmpeg", "-n", "-hide_banner",
            "-i", self.pw_source,
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            # Drain stderr so the pipe never fills
            def _drain(p):
                fd = p.stderr.fileno()
                try:
                    while True:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                except Exception:
                    pass
            threading.Thread(target=_drain, args=(self._proc,),
                             daemon=True).start()
            # Wait until process exits or we are stopped
            while self._running and self._proc.poll() is None:
                self.msleep(100)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self._terminate_nowait()

    def stop(self):
        self._running = False
        self._terminate_nowait()

    def _terminate_nowait(self):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

# ══════════════════════════════════════════════════════════════
#  PreviewWorker  — raw RGB frames from ffmpeg stdout
# ══════════════════════════════════════════════════════════════

class PreviewWorker(QThread):
    """
    Spawns:  ffmpeg ... -f rawvideo -pix_fmt rgb24 pipe:1
             OR a GStreamer pipeline producing the same raw RGB stream.
    Reads exactly (w*h*3) bytes per frame from stdout.
    Emits frame_ready(QImage) via queued connection → main thread paints it.
    Stderr is consumed by a daemon thread to prevent pipe stall.

    Freeze-free shutdown contract
    ─────────────────────────────
    stop() sets _running=False and sends SIGTERM to the child process.
    It does NOT wait() — that would block the caller.  The run() loop
    notices either _running==False or the select() timeout + poll()==done
    and exits cleanly on its own, after which the QThread finishes and
    the finished() signal fires.  The main thread uses a QTimer to poll
    isRunning() rather than a blocking wait().
    """
    frame_ready  = pyqtSignal(QImage)
    error        = pyqtSignal(str)
    stderr_line  = pyqtSignal(str)   # ffmpeg diagnostic lines → app log

    def __init__(self, cmd, width, height):
        super().__init__()
        self.cmd      = cmd
        self.width    = width
        self.height   = height
        self._proc    = None
        self._running = True

    def run(self):
        frame_bytes = self.width * self.height * 3
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            # Drain stderr via os.read() — line-iterator would block on \r
            # progress lines that ffmpeg writes without \n.
            # We split on \r and \n and emit meaningful lines to the app log.
            def _drain(p):
                fd = p.stderr.fileno()
                buf = b""
                try:
                    while True:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf or b"\r" in buf:
                            for sep in (b"\n", b"\r"):
                                if sep in buf:
                                    line, buf = buf.split(sep, 1)
                                    txt = line.decode("utf-8", errors="replace").strip()
                                    if txt and not txt.startswith("frame="):
                                        self.stderr_line.emit(txt)
                                    break
                except Exception:
                    pass
            threading.Thread(target=_drain, args=(self._proc,),
                             daemon=True).start()

            buf = bytearray()
            while self._running:
                ready, _, _ = select.select([self._proc.stdout], [], [], 0.05)
                if not ready:
                    if self._proc.poll() is not None:
                        break
                    continue
                chunk = os.read(self._proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= frame_bytes:
                    raw = bytes(buf[:frame_bytes])
                    del buf[:frame_bytes]
                    img = QImage(raw, self.width, self.height,
                                 self.width * 3, QImage.Format.Format_RGB888)
                    self.frame_ready.emit(img.copy())
        except Exception as e:
            self.error.emit(str(e))
        finally:
            # Terminate without waiting — never block the worker thread
            self._terminate_nowait()

    def stop(self):
        """Signal the run loop to exit and kill the child. Non-blocking."""
        self._running = False
        self._terminate_nowait()

    def _terminate_nowait(self):
        """
        SIGTERM the child process and schedule a SIGKILL if it is still
        alive after 1 second.  Never waits — the kill is dispatched via a
        daemon thread so this method returns immediately.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            return
        # Escalate to SIGKILL after 1 s if SIGTERM wasn't enough
        def _kill_if_alive(p):
            import time
            time.sleep(1.0)
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
        threading.Thread(target=_kill_if_alive, args=(proc,),
                         daemon=True).start()

# ══════════════════════════════════════════════════════════════
#  GstPreviewWorker  — pipewiresrc → raw RGB frames
# ══════════════════════════════════════════════════════════════

class GstPreviewWorker(QThread):
    """
    Uses gst-launch-1.0 to pull frames from a PipeWire Video/Source node
    (libcamera/IPU6 cameras that ffmpeg cannot open directly).

    Pipeline:
      pipewiresrc target-object=<pw_node_id> do-timestamp=true
        ! videoconvert
        ! video/x-raw,format=RGB,width=W,height=H,framerate=FPS/1
        ! fdsink fd=1

    Frames arrive as raw packed RGB24, same format as PreviewWorker,
    so the same PreviewWindow / paintEvent code handles both.

    Shares the same non-blocking shutdown contract as PreviewWorker:
    stop() sends SIGTERM only, never waits.
    """
    frame_ready = pyqtSignal(QImage)
    error       = pyqtSignal(str)

    def __init__(self, pw_node_id: int, width: int, height: int, fps: str,
                 flip_method: int = 0, brightness: float = 0.0,
                 contrast: float = 1.0, saturation: float = 1.0):
        super().__init__()
        self.pw_node_id  = pw_node_id
        self.width       = width
        self.height      = height
        self.fps         = fps
        self.flip_method = flip_method
        self.brightness  = brightness
        self.contrast    = contrast
        self.saturation  = saturation
        self._proc       = None
        self._running    = True

    def run(self):
        frame_bytes = self.width * self.height * 3
        # Build the GStreamer pipeline
        # framerate filter prevents negotiation hangs on cameras that
        # prefer non-standard rates
        fps_int = self.fps.split(".")[0]
        caps = (f"video/x-raw,format=RGB"
                f",width={self.width},height={self.height}"
                f",framerate={fps_int}/1")
        # Build pipeline elements
        pipeline = [
            "pipewiresrc",
                f"target-object={self.pw_node_id}",
                "do-timestamp=true",
        ]
        # Flip/rotate via videoflip (method 0 = identity, still add for resize)
        if self.flip_method != 0:
            pipeline += ["!", "videoflip", f"method={self.flip_method}"]
        # Colour balance
        if self.brightness != 0.0 or self.contrast != 1.0 or self.saturation != 1.0:
            pipeline += [
                "!", "videobalance",
                f"brightness={self.brightness:.3f}",
                f"contrast={self.contrast:.3f}",
                f"saturation={self.saturation:.3f}",
            ]
        pipeline += ["!", "videoconvert", "!", caps, "!", "fdsink", "fd=1"]
        cmd = ["gst-launch-1.0", "-q"] + pipeline
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            def _drain(p):
                fd = p.stderr.fileno()
                try:
                    while True:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                except Exception:
                    pass
            threading.Thread(target=_drain, args=(self._proc,),
                             daemon=True).start()

            buf = bytearray()
            while self._running:
                ready, _, _ = select.select([self._proc.stdout], [], [], 0.05)
                if not ready:
                    if self._proc.poll() is not None:
                        break
                    continue
                chunk = os.read(self._proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= frame_bytes:
                    raw = bytes(buf[:frame_bytes])
                    del buf[:frame_bytes]
                    img = QImage(raw, self.width, self.height,
                                 self.width * 3, QImage.Format.Format_RGB888)
                    self.frame_ready.emit(img.copy())
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self._terminate_nowait()

    def stop(self):
        self._running = False
        self._terminate_nowait()

    def _terminate_nowait(self):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

# ══════════════════════════════════════════════════════════════
#  CaptureWorker  — encoding ffmpeg process
# ══════════════════════════════════════════════════════════════

# Preview frame dimensions used for the capture monitor tee
_MON_W, _MON_H = 320, 240


class CaptureWorker(QThread):
    """
    Runs the encode ffmpeg command.

    If the command includes a rawvideo pipe:1 monitor output (added by
    _build_capture_cmd when a video device is open), stdout carries
    320×240 RGB24 frames that are emitted as frame_ready signals so the
    VideoPane can show a live preview during recording.

    stderr carries ffmpeg progress/log lines as before.
    Both pipes are read with select() — never blocking, always stoppable.
    """
    log_line    = pyqtSignal(str)
    time_tick   = pyqtSignal(str)
    frame_ready = pyqtSignal(QImage)
    finished    = pyqtSignal(int)

    def __init__(self, cmd, monitor_frames: bool = False):
        super().__init__()
        self.cmd            = cmd
        self.monitor_frames = monitor_frames
        self._proc          = None
        self._running       = True

    def run(self):
        self.log_line.emit("$ " + " ".join(self.cmd))
        frame_bytes = _MON_W * _MON_H * 3
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE if self.monitor_frames
                       else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            vid_buf  = bytearray()
            leftover = b""
            fds = [self._proc.stderr]
            if self.monitor_frames:
                fds.append(self._proc.stdout)

            while self._running:
                ready, _, _ = select.select(fds, [], [], 0.05)
                if not ready:
                    if self._proc.poll() is not None:
                        break
                    continue

                # ── stderr: log + timecode ────────────────────────────
                if self._proc.stderr in ready:
                    chunk = os.read(self._proc.stderr.fileno(), 4096)
                    if chunk:
                        data  = leftover + chunk
                        parts = data.replace(b"\r", b"\n").split(b"\n")
                        leftover = parts[-1]
                        for part in parts[:-1]:
                            line = part.decode(errors="replace").strip()
                            if not line:
                                continue
                            self.log_line.emit(line)
                            if "time=" in line:
                                for tok in line.split():
                                    if tok.startswith("time="):
                                        self.time_tick.emit(tok[5:])

                # ── stdout: rawvideo monitor frames ───────────────────
                if self.monitor_frames and self._proc.stdout in ready:
                    chunk = os.read(self._proc.stdout.fileno(), 65536)
                    if chunk:
                        vid_buf.extend(chunk)
                        while len(vid_buf) >= frame_bytes:
                            raw = bytes(vid_buf[:frame_bytes])
                            del vid_buf[:frame_bytes]
                            img = QImage(raw, _MON_W, _MON_H,
                                         _MON_W * 3,
                                         QImage.Format.Format_RGB888)
                            self.frame_ready.emit(img.copy())

            if leftover:
                line = leftover.decode(errors="replace").strip()
                if line:
                    self.log_line.emit(line)
            self._proc.wait()
            self.finished.emit(self._proc.returncode)
        except Exception as e:
            self.log_line.emit(f"[ERROR] {e}")
            self.finished.emit(-1)

    def stop(self):
        self._running = False
        if self._proc and self._proc.poll() is None:
            try: self._proc.terminate()
            except Exception: pass

# ══════════════════════════════════════════════════════════════
#  VideoPane  — embedded preview panel inside the main window
# ══════════════════════════════════════════════════════════════

class VideoPane(QWidget):
    """
    Letterboxed video preview embedded in the main window splitter.
    Shows a HUD overlay with REC badge and elapsed time.
    Double-click or [F] to go fullscreen; [Esc] to return.
    """
    sig_record = pyqtSignal()
    sig_stop   = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setStyleSheet("background:#000;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._pixmap       = QPixmap(640, 480)
        self._pixmap.fill(QColor(10, 10, 10))
        self._is_recording = False
        self._elapsed      = "00:00:00"
        self._active       = False   # True while a preview worker is running
        self._hud_visible  = True

        self._hud_timer = QTimer(self)
        self._hud_timer.setSingleShot(True)
        self._hud_timer.timeout.connect(self._hide_hud)
        self.setMouseTracking(True)

    # ── public slots ──────────────────────────────────────────

    def update_frame(self, img: QImage):
        self._pixmap = QPixmap.fromImage(img)
        self._active = True
        self.update()

    def set_recording(self, rec: bool):
        self._is_recording = rec
        self._hud_visible  = True
        self.update()

    def set_elapsed(self, t: str):
        self._elapsed = t
        self.update()

    def set_active(self, active: bool):
        """Call with False when preview stops so the pane shows idle state."""
        self._active = active
        if not active:
            self._pixmap = QPixmap(self.width() or 640, self.height() or 480)
            self._pixmap.fill(QColor(10, 10, 10))
        self.update()

    # ── painting ──────────────────────────────────────────────

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        W, H = self.width(), self.height()

        # Letterbox video onto black background
        scaled = self._pixmap.scaled(
            W, H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (W - scaled.width())  // 2
        y = (H - scaled.height()) // 2
        p.fillRect(0, 0, W, H, QColor(0, 0, 0))
        p.drawPixmap(x, y, scaled)

        if not self._active:
            # Idle — draw centred placeholder text
            p.setPen(QPen(QColor(50, 50, 52)))
            p.setFont(QFont("IBM Plex Mono", 13))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "● PRESS PREVIEW TO START")
        elif self._hud_visible:
            self._draw_hud(p, W, H)
        p.end()

    def _draw_hud(self, p: QPainter, W: int, H: int):
        HUD_H = 56
        p.fillRect(0, H - HUD_H, W, HUD_H, QColor(0, 0, 0, 168))
        p.setPen(QPen(QColor(232, 160, 32), 1))
        p.drawLine(0, H - HUD_H, W, H - HUD_H)

        big   = QFont("IBM Plex Mono", 18); big.setBold(True)
        small = QFont("IBM Plex Mono", 9)

        if self._is_recording:
            p.setPen(QPen(QColor(217, 64, 64)))
            p.setFont(QFont("IBM Plex Mono", 10, QFont.Weight.Bold))
            p.drawText(QRect(10, H - HUD_H + 6, 52, 18),
                       Qt.AlignmentFlag.AlignLeft, "● REC")
            p.setPen(QPen(QColor(232, 160, 32)))
            p.setFont(big)
            p.drawText(QRect(0, H - HUD_H + 2, W, 34),
                       Qt.AlignmentFlag.AlignHCenter, self._elapsed)
            p.setPen(QPen(QColor(140, 138, 130)))
            p.setFont(small)
            p.drawText(QRect(0, H - 16, W, 14),
                       Qt.AlignmentFlag.AlignHCenter,
                       "[ S ] stop   [ F ] fullscreen")
        else:
            p.setPen(QPen(QColor(90, 88, 82)))
            p.setFont(QFont("IBM Plex Mono", 10))
            p.drawText(QRect(0, H - HUD_H + 10, W, 20),
                       Qt.AlignmentFlag.AlignHCenter, "LIVE PREVIEW")
            p.setPen(QPen(QColor(140, 138, 130)))
            p.setFont(small)
            p.drawText(QRect(0, H - 16, W, 14),
                       Qt.AlignmentFlag.AlignHCenter,
                       "[ R ] record   [ F ] fullscreen")

    # ── input ─────────────────────────────────────────────────

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_R and not self._is_recording:
            self.sig_record.emit()
        elif k == Qt.Key.Key_S and self._is_recording:
            self.sig_stop.emit()
        elif k == Qt.Key.Key_F:
            self._toggle_fullscreen()
        elif k == Qt.Key.Key_Escape:
            self._exit_fullscreen()
        else:
            super().keyPressEvent(ev)

    def mouseDoubleClickEvent(self, _ev):
        self._toggle_fullscreen()

    def mouseMoveEvent(self, _ev):
        if not self._hud_visible:
            self._hud_visible = True
            self.update()
        self._hud_timer.start(3000)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()

    def _hide_hud(self):
        self._hud_visible = False
        self.update()

# ══════════════════════════════════════════════════════════════
#  Main Window
# ══════════════════════════════════════════════════════════════

class VideoCapture(QMainWindow):
    def __init__(self):
        super().__init__()
        self.capture_worker  = None
        self.preview_worker  = None
        self.monitor_worker  = None    # AudioMonitorWorker — runs during preview
        self.scan_worker     = None
        self.probe_worker    = None    # ProbeWorker — encoder/device probing
        self._preview_active = False   # True while preview worker is alive
        self._device_formats: dict[str, list[str]] = {}
        self.setWindowTitle("VIDEO CAPTURE")
        self.setMinimumSize(960, 620)
        self._build_ui()
        self._refresh_devices()

    # ─────────────────────────── UI ──────────────────────────

    def _build_ui(self):
        self.setStyleSheet(QSS)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ── Header bar ───────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("⏺  VIDEO CAPTURE")
        title.setStyleSheet(
            f"color:{ACCENT};font-size:17px;font-weight:bold;letter-spacing:4px;")
        hdr.addWidget(title)
        hdr.addStretch()
        self.lbl_status = QLabel("IDLE")
        self.lbl_status.setStyleSheet(
            f"color:{TEXT_SEC};font-size:11px;letter-spacing:2px;")
        hdr.addWidget(self.lbl_status)
        root.addLayout(hdr)

        sep = QFrame(); sep.setFixedHeight(1)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{BORDER};")
        root.addWidget(sep)

        # ── Main splitter: video pane (left) + controls (right) ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(6)
        splitter.setStyleSheet(
            "QSplitter::handle { background: #2a2b2e; }")
        root.addWidget(splitter, 1)

        # Left: VideoPane
        self.video_pane = VideoPane()
        self.video_pane.sig_record.connect(self._start_capture)
        self.video_pane.sig_stop.connect(self._stop_capture)
        self.video_pane.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        splitter.addWidget(self.video_pane)

        # Right: controls panel
        ctrl = QWidget()
        ctrl.setMinimumWidth(340)
        ctrl.setMaximumWidth(520)
        ctrl_layout = QVBoxLayout(ctrl)
        ctrl_layout.setContentsMargins(8, 4, 4, 4)
        ctrl_layout.setSpacing(6)

        tabs = QTabWidget()
        ctrl_layout.addWidget(tabs, 1)
        tabs.addTab(self._tab_capture(),  "CAPTURE")
        tabs.addTab(self._tab_codec(),    "CODEC")
        tabs.addTab(self._tab_video(),    "VIDEO")
        tabs.addTab(self._tab_advanced(), "ADVANCED")
        tabs.addTab(self._tab_log(),      "LOG")

        # Buttons row inside controls
        bot = QHBoxLayout()
        self.btn_refresh = QPushButton("↻ SCAN")
        self.btn_refresh.clicked.connect(self._refresh_devices)
        bot.addWidget(self.btn_refresh)

        self.btn_preview = QPushButton("◉  PREVIEW")
        self.btn_preview.clicked.connect(self._toggle_preview)
        bot.addWidget(self.btn_preview)

        bot.addStretch()
        self.lbl_timer = QLabel("00:00:00")
        self.lbl_timer.setStyleSheet(
            f"color:{ACCENT};font-size:20px;font-weight:bold;letter-spacing:3px;")
        bot.addWidget(self.lbl_timer)
        ctrl_layout.addLayout(bot)

        # REC / STOP row
        rec = QHBoxLayout(); rec.setSpacing(8)
        self.btn_record = QPushButton("● REC")
        self.btn_record.setObjectName("record")
        self.btn_record.setMinimumHeight(44)
        self.btn_record.clicked.connect(self._start_capture)
        rec.addWidget(self.btn_record)

        self.btn_stop = QPushButton("■ STOP")
        self.btn_stop.setObjectName("stop")
        self.btn_stop.setMinimumHeight(44)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_capture)
        rec.addWidget(self.btn_stop)
        ctrl_layout.addLayout(rec)

        splitter.addWidget(ctrl)
        splitter.setSizes([560, 380])  # initial split

        self.statusBar().showMessage("Ready — select device, then ◉ PREVIEW or ● REC")

    # ── Capture tab ───────────────────────────────────────────

    def _tab_capture(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setSpacing(10); v.setContentsMargins(8, 8, 8, 8)

        grp = QGroupBox("Video Source")
        g = QGridLayout(grp); g.setHorizontalSpacing(12); g.setVerticalSpacing(8)

        g.addWidget(QLabel("Device"), 0, 0)
        self.cmb_device = QComboBox()
        self.cmb_device.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cmb_device.currentIndexChanged.connect(self._on_device_changed)
        g.addWidget(self.cmb_device, 0, 1, 1, 3)

        g.addWidget(QLabel("Input Format"), 1, 0)
        self.cmb_v4l2fmt = QComboBox()
        g.addWidget(self.cmb_v4l2fmt, 1, 1)

        g.addWidget(QLabel("Resolution"), 1, 2)
        self.cmb_resolution = QComboBox()
        for r in ["720x480","720x576","640x480","1280x720","1920x1080"]:
            self.cmb_resolution.addItem(r)
        g.addWidget(self.cmb_resolution, 1, 3)

        g.addWidget(QLabel("Framerate"), 2, 0)
        self.cmb_fps = QComboBox()
        for fps in ["29.97","25","30","60","15"]:
            self.cmb_fps.addItem(fps)
        g.addWidget(self.cmb_fps, 2, 1)

        g.addWidget(QLabel("Deinterlace"), 2, 2)
        self.chk_deinterlace = QCheckBox("yadif")
        self.chk_deinterlace.setChecked(True)
        g.addWidget(self.chk_deinterlace, 2, 3)
        v.addWidget(grp)

        grp2 = QGroupBox("Audio Source")
        ga = QGridLayout(grp2); ga.setHorizontalSpacing(12); ga.setVerticalSpacing(8)

        ga.addWidget(QLabel("Device"), 0, 0)
        self.cmb_audio_dev = QComboBox()
        self.cmb_audio_dev.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ga.addWidget(self.cmb_audio_dev, 0, 1, 1, 3)

        self.chk_monitor = QCheckBox("Play audio through speakers during preview & recording")
        self.chk_monitor.setChecked(True)
        ga.addWidget(self.chk_monitor, 1, 0, 1, 4)
        v.addWidget(grp2)

        grp3 = QGroupBox("Output File")
        go = QHBoxLayout(grp3)
        self.txt_output = QLineEdit()
        default = datetime.now().strftime("capture_%Y%m%d_%H%M%S.webm")
        self.txt_output.setText(
            os.path.join(os.path.expanduser("~"), "Videos", default))
        go.addWidget(self.txt_output)
        btn_b = QPushButton("Browse …")
        btn_b.clicked.connect(self._browse_output)
        go.addWidget(btn_b)
        v.addWidget(grp3)
        v.addStretch()
        return w

    # ── Codec tab ─────────────────────────────────────────────

    def _tab_codec(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setSpacing(10); v.setContentsMargins(8, 8, 8, 8)

        grp = QGroupBox("Video Codec (AV1)")
        g = QGridLayout(grp); g.setHorizontalSpacing(12); g.setVerticalSpacing(8)

        g.addWidget(QLabel("Encoder"), 0, 0)
        self.cmb_vcodec = QComboBox()
        g.addWidget(self.cmb_vcodec, 0, 1, 1, 3)

        g.addWidget(QLabel("CQ / CRF"), 1, 0)
        self.spn_crf = QSpinBox()
        self.spn_crf.setRange(0, 63); self.spn_crf.setValue(28)
        g.addWidget(self.spn_crf, 1, 1)

        g.addWidget(QLabel("Bitrate (kb/s)"), 2, 0)
        self.spn_vbitrate = QSpinBox()
        self.spn_vbitrate.setRange(0, 50000); self.spn_vbitrate.setValue(0)
        self.spn_vbitrate.setSpecialValueText("VBR (0=off)")
        g.addWidget(self.spn_vbitrate, 2, 1)

        g.addWidget(QLabel("Speed Preset"), 3, 0)
        self.cmb_preset = QComboBox()
        for p in ["0 (slowest)","1","2","3","4","5","6","7","8 (fastest)"]:
            self.cmb_preset.addItem(p)
        self.cmb_preset.setCurrentIndex(6)
        g.addWidget(self.cmb_preset, 3, 1)

        g.addWidget(QLabel("Tile Columns"), 4, 0)
        self.spn_tiles = QSpinBox()
        self.spn_tiles.setRange(0, 6); self.spn_tiles.setValue(2)
        g.addWidget(self.spn_tiles, 4, 1)

        g.addWidget(QLabel("Extra v:opts"), 5, 0)
        self.txt_vextra = QLineEdit()
        self.txt_vextra.setPlaceholderText("-row-mt 1  -g 120")
        g.addWidget(self.txt_vextra, 5, 1, 1, 3)
        v.addWidget(grp)

        grp2 = QGroupBox("Audio Codec (Opus)")
        ga = QGridLayout(grp2); ga.setHorizontalSpacing(12); ga.setVerticalSpacing(8)

        ga.addWidget(QLabel("Encoder"), 0, 0)
        self.cmb_acodec = QComboBox()
        ga.addWidget(self.cmb_acodec, 0, 1, 1, 3)

        ga.addWidget(QLabel("Bitrate (kb/s)"), 1, 0)
        self.spn_abitrate = QSpinBox()
        self.spn_abitrate.setRange(8, 512); self.spn_abitrate.setValue(128)
        ga.addWidget(self.spn_abitrate, 1, 1)

        ga.addWidget(QLabel("VBR Mode"), 1, 2)
        self.cmb_opus_vbr = QComboBox()
        for m in ["on","off","constrained"]: self.cmb_opus_vbr.addItem(m)
        ga.addWidget(self.cmb_opus_vbr, 1, 3)

        ga.addWidget(QLabel("Compression"), 2, 0)
        self.spn_compression = QSpinBox()
        self.spn_compression.setRange(0, 10); self.spn_compression.setValue(10)
        ga.addWidget(self.spn_compression, 2, 1)

        ga.addWidget(QLabel("Extra a:opts"), 3, 0)
        self.txt_aextra = QLineEdit()
        self.txt_aextra.setPlaceholderText("-application voip")
        ga.addWidget(self.txt_aextra, 3, 1, 1, 3)
        v.addWidget(grp2)

        grp3 = QGroupBox("Container")
        gc = QGridLayout(grp3)
        gc.addWidget(QLabel("Format"), 0, 0)
        self.cmb_container = QComboBox()
        for c in ["webm","mkv","mp4","avi"]: self.cmb_container.addItem(c)
        gc.addWidget(self.cmb_container, 0, 1)
        v.addWidget(grp3)
        v.addStretch()
        return w

    # ── Video tab — orientation, zoom, colour ────────────────

    def _tab_video(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setSpacing(10); v.setContentsMargins(8, 8, 8, 8)

        # ── Orientation ──────────────────────────────────────
        grp_orient = QGroupBox("Orientation")
        go = QGridLayout(grp_orient)
        go.setHorizontalSpacing(12); go.setVerticalSpacing(8)

        go.addWidget(QLabel("Rotate"), 0, 0)
        self.cmb_rotate = QComboBox()
        for lbl in ["0°", "90° CW", "180°", "90° CCW"]:
            self.cmb_rotate.addItem(lbl)
        go.addWidget(self.cmb_rotate, 0, 1)

        go.addWidget(QLabel("Flip"), 1, 0)
        self.chk_flip_h = QCheckBox("Horizontal")
        self.chk_flip_v = QCheckBox("Vertical")
        fbox = QHBoxLayout()
        fbox.addWidget(self.chk_flip_h)
        fbox.addWidget(self.chk_flip_v)
        fbox.addStretch()
        go.addLayout(fbox, 1, 1)

        v.addWidget(grp_orient)

        # ── Zoom / Crop ───────────────────────────────────────
        grp_zoom = QGroupBox("Zoom  (crops centre region)")
        gz = QGridLayout(grp_zoom)
        gz.setHorizontalSpacing(12); gz.setVerticalSpacing(8)

        gz.addWidget(QLabel("Zoom %"), 0, 0)
        self.sld_zoom = QSlider(Qt.Orientation.Horizontal)
        self.sld_zoom.setRange(100, 300)   # 100% = no zoom, 200% = 2× crop
        self.sld_zoom.setValue(100)
        self.sld_zoom.setTickInterval(25)
        self.sld_zoom.setTickPosition(QSlider.TickPosition.TicksBelow)
        gz.addWidget(self.sld_zoom, 0, 1)
        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setFixedWidth(40)
        gz.addWidget(self.lbl_zoom, 0, 2)
        self.sld_zoom.valueChanged.connect(
            lambda v: self.lbl_zoom.setText(f"{v}%"))

        v.addWidget(grp_zoom)

        # ── Colour / Levels ───────────────────────────────────
        grp_col = QGroupBox("Colour  (ffmpeg eq filter — preview + encode)")
        gc = QGridLayout(grp_col)
        gc.setHorizontalSpacing(12); gc.setVerticalSpacing(8)

        def _slider_row(label, row, lo, hi, default, scale, fmt):
            """Helper: add label + slider + value label to gc."""
            gc.addWidget(QLabel(label), row, 0)
            sld = QSlider(Qt.Orientation.Horizontal)
            sld.setRange(lo, hi)
            sld.setValue(default)
            gc.addWidget(sld, row, 1)
            lbl = QLabel(fmt.format(default / scale))
            lbl.setFixedWidth(48)
            gc.addWidget(lbl, row, 2)
            sld.valueChanged.connect(
                lambda val, l=lbl, sc=scale, f=fmt: l.setText(f.format(val / sc)))
            return sld

        # brightness: –1.0 … +1.0, stored ×100 as int
        self.sld_brightness = _slider_row(
            "Brightness", 0, -100, 100, 0, 100.0, "{:+.2f}")
        # contrast: 0.0 … 3.0, stored ×100
        self.sld_contrast = _slider_row(
            "Contrast",   1,    0, 300, 100, 100.0, "{:.2f}")
        # saturation: 0.0 … 3.0, stored ×100
        self.sld_saturation = _slider_row(
            "Saturation", 2,    0, 300, 100, 100.0, "{:.2f}")
        # gamma: 0.1 … 10.0, stored ×10
        self.sld_gamma = _slider_row(
            "Gamma",      3,    1, 100,  10,  10.0, "{:.1f}")

        btn_reset = QPushButton("↺  Reset colour")
        btn_reset.clicked.connect(self._reset_colour)
        gc.addWidget(btn_reset, 4, 0, 1, 3)

        v.addWidget(grp_col)
        v.addStretch()
        return w

    def _reset_colour(self):
        self.sld_brightness.setValue(0)
        self.sld_contrast.setValue(100)
        self.sld_saturation.setValue(100)
        self.sld_gamma.setValue(10)

    def _auto_orient_for_device(self, dev_info):
        """
        Apply sensible orientation defaults when the user picks a device.
        IPU6 (libcamera/PipeWire) reports location=front and mounts
        upside-down on most Alder Lake laptops — default to 180° rotate.
        """
        if dev_info and dev_info.get("backend") == "pipewire":
            self.cmb_rotate.setCurrentIndex(2)   # 180°
        else:
            self.cmb_rotate.setCurrentIndex(0)   # 0°

    def _build_vf(self, for_preview=False):
        """
        Build the complete ffmpeg -vf filter string from the VIDEO tab
        controls plus the manual filter chain in the ADVANCED tab.

        Order: deinterlace → crop/zoom → flip/rotate → eq (colour)

        Returns "" if no filters are needed.
        """
        filters = []

        # Deinterlace (only for V4L2 capture; skip for GStreamer preview)
        manual = self.txt_vfilter.text().strip()
        if manual:
            # Manual chain overrides everything except rotate/flip/eq
            filters.append(manual)
        elif self.chk_deinterlace.isChecked():
            filters.append("yadif=mode=1")

        # Zoom via crop + scale
        zoom = self.sld_zoom.value()
        if zoom > 100:
            # Crop a 1/zoom-fraction of the input, then scale back up
            # Use iw/ih variables so the filter works at any input resolution
            filters.append(
                f"crop=iw*100/{zoom}:ih*100/{zoom},scale=iw*{zoom}/100:ih*{zoom}/100")

        # Horizontal / vertical flip
        hflip = self.chk_flip_h.isChecked()
        vflip = self.chk_flip_v.isChecked()
        if hflip and vflip:
            filters.append("hflip,vflip")
        elif hflip:
            filters.append("hflip")
        elif vflip:
            filters.append("vflip")

        # Rotation  (transpose pairs for 90/270; vflip+hflip for 180)
        rot_idx = self.cmb_rotate.currentIndex()
        if rot_idx == 1:     # 90° CW
            filters.append("transpose=1")
        elif rot_idx == 2:   # 180°
            filters.append("transpose=2,transpose=2")
        elif rot_idx == 3:   # 90° CCW
            filters.append("transpose=2")

        # Colour eq (omit if all defaults)
        br  = self.sld_brightness.value() / 100.0
        con = self.sld_contrast.value()   / 100.0
        sat = self.sld_saturation.value() / 100.0
        gam = self.sld_gamma.value()      / 10.0
        if br != 0.0 or con != 1.0 or sat != 1.0 or gam != 1.0:
            filters.append(
                f"eq=brightness={br:.3f}:contrast={con:.3f}"
                f":saturation={sat:.3f}:gamma={gam:.2f}")

        return ",".join(filters)

    def _build_gst_videoflip(self):
        """
        Return (flip_method_int, videobalance_props) for GStreamer pipeline.
        videoflip method:  0=none,1=CW90,2=180,3=CCW90,4=hflip,5=vflip,6=ul-lr,7=ur-ll
        We combine rotate + flip into the nearest videoflip method.
        Colour is handled by videobalance.
        """
        rot   = self.cmb_rotate.currentIndex()   # 0,1,2,3
        hflip = self.chk_flip_h.isChecked()
        vflip = self.chk_flip_v.isChecked()

        # Map rotate to base videoflip method
        rot_map = {0: 0, 1: 1, 2: 2, 3: 3}
        method = rot_map[rot]

        # If only one flip axis is set (and no rotate), use videoflip flip modes
        if not rot and hflip and not vflip:
            method = 4
        elif not rot and vflip and not hflip:
            method = 5
        # Both flips = 180 rotate (same as rot=2)
        elif not rot and hflip and vflip:
            method = 2

        br  = self.sld_brightness.value() / 100.0  # –1…+1 maps to videobalance –1…+1
        con = self.sld_contrast.value()   / 100.0  # videobalance contrast 0…2
        sat = self.sld_saturation.value() / 100.0  # videobalance saturation 0…2
        return method, br, con, sat

    # ── Advanced tab ──────────────────────────────────────────

    def _tab_advanced(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setSpacing(10); v.setContentsMargins(8, 8, 8, 8)

        grp = QGroupBox("ffmpeg / V4L2 Advanced")
        g = QGridLayout(grp); g.setHorizontalSpacing(12); g.setVerticalSpacing(8)

        g.addWidget(QLabel("Thread Queue Size"), 0, 0)
        self.spn_thread_q = QSpinBox()
        self.spn_thread_q.setRange(1, 8192); self.spn_thread_q.setValue(512)
        g.addWidget(self.spn_thread_q, 0, 1)

        g.addWidget(QLabel("V4L2 Buffers"), 1, 0)
        self.spn_buffers = QSpinBox()
        self.spn_buffers.setRange(1, 32); self.spn_buffers.setValue(4)
        g.addWidget(self.spn_buffers, 1, 1)

        g.addWidget(QLabel("A/V Sync Offset (s)"), 2, 0)
        self.spn_avsync = QDoubleSpinBox()
        self.spn_avsync.setRange(-5, 5); self.spn_avsync.setSingleStep(0.05)
        g.addWidget(self.spn_avsync, 2, 1)

        g.addWidget(QLabel("Video Filter Chain"), 3, 0)
        self.txt_vfilter = QLineEdit()
        self.txt_vfilter.setPlaceholderText(
            "yadif=mode=1,hqdn3d,unsharp  (overrides checkbox)")
        g.addWidget(self.txt_vfilter, 3, 1, 1, 3)

        g.addWidget(QLabel("Extra Global Opts"), 4, 0)
        self.txt_extra_global = QLineEdit()
        self.txt_extra_global.setPlaceholderText("-loglevel verbose  -stats")
        g.addWidget(self.txt_extra_global, 4, 1, 1, 3)

        btn_reset_settings = QPushButton("⚠  Reset All Settings to Defaults")
        btn_reset_settings.clicked.connect(self._reset_settings)
        g.addWidget(btn_reset_settings, 5, 0, 1, 4)
        v.addWidget(grp)

        grp2 = QGroupBox("Command Preview")
        gp = QVBoxLayout(grp2)
        btn_prev = QPushButton("↺  Refresh")
        btn_prev.clicked.connect(self._update_preview)
        gp.addWidget(btn_prev)
        self.lbl_preview = QLabel("")
        self.lbl_preview.setStyleSheet(f"color:{ACCENT2};font-size:10px;")
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        gp.addWidget(self.lbl_preview)
        v.addWidget(grp2)
        v.addStretch()
        return w

    # ── Log tab ───────────────────────────────────────────────

    def _tab_log(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(8, 8, 8, 8)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        v.addWidget(self.log_view)
        btn_c = QPushButton("Clear Log")
        btn_c.clicked.connect(self.log_view.clear)
        v.addWidget(btn_c)
        return w

    # ─────────────────────────── Settings persistence ────────

    _SETTINGS_ORG  = "video-capture"
    _SETTINGS_APP  = "video-capture"

    def _load_settings(self):
        """Restore all widget values from QSettings (silently ignore missing keys)."""
        s = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)

        def _cmb(w, key):
            v = s.value(key)
            if v is not None:
                idx = w.findText(str(v))
                if idx >= 0:
                    w.setCurrentIndex(idx)

        def _cmb_data(w, key):
            """Select combo item whose data matches saved value."""
            v = s.value(key)
            if v is not None:
                for i in range(w.count()):
                    if str(w.itemData(i)) == str(v):
                        w.setCurrentIndex(i)
                        return
                # fallback: match by text prefix
                idx = w.findText(str(v))
                if idx >= 0:
                    w.setCurrentIndex(idx)

        def _spn(w, key):
            v = s.value(key)
            if v is not None:
                try: w.setValue(type(w.value())(float(v)))
                except Exception: pass

        def _chk(w, key):
            v = s.value(key)
            if v is not None:
                w.setChecked(str(v).lower() in ("true", "1", "yes"))

        def _txt(w, key):
            v = s.value(key)
            if v is not None:
                w.setText(str(v))

        def _sld(w, key):
            v = s.value(key)
            if v is not None:
                try: w.setValue(int(v))
                except Exception: pass

        # CAPTURE tab
        _cmb(self.cmb_v4l2fmt,   "cap/fmt")
        _cmb(self.cmb_resolution,"cap/res")
        _cmb(self.cmb_fps,       "cap/fps")
        _chk(self.chk_deinterlace,"cap/deinterlace")
        _cmb_data(self.cmb_audio_dev, "cap/audio_dev")
        _chk(self.chk_monitor,   "cap/monitor")
        # Output: only restore directory, not the timestamped filename
        saved_dir = s.value("cap/output_dir")
        if saved_dir:
            ts = datetime.now().strftime("capture_%Y%m%d_%H%M%S.webm")
            self.txt_output.setText(os.path.join(saved_dir, ts))

        # CODEC tab
        _cmb_data(self.cmb_vcodec,    "codec/venc")
        _spn(self.spn_crf,            "codec/crf")
        _spn(self.spn_vbitrate,       "codec/vbitrate")
        _cmb(self.cmb_preset,         "codec/preset")
        _spn(self.spn_tiles,          "codec/tiles")
        _txt(self.txt_vextra,         "codec/vextra")
        _cmb_data(self.cmb_acodec,    "codec/aenc")
        _spn(self.spn_abitrate,       "codec/abitrate")
        _cmb(self.cmb_opus_vbr,       "codec/opus_vbr")
        _spn(self.spn_compression,    "codec/compression")
        _txt(self.txt_aextra,         "codec/aextra")
        _cmb(self.cmb_container,      "codec/container")

        # VIDEO tab
        _cmb(self.cmb_rotate,         "video/rotate")
        _chk(self.chk_flip_h,         "video/flip_h")
        _chk(self.chk_flip_v,         "video/flip_v")
        _sld(self.sld_zoom,           "video/zoom")
        _sld(self.sld_brightness,     "video/brightness")
        _sld(self.sld_contrast,       "video/contrast")
        _sld(self.sld_saturation,     "video/saturation")
        _sld(self.sld_gamma,          "video/gamma")

        # ADVANCED tab
        _spn(self.spn_thread_q,       "adv/thread_q")
        _spn(self.spn_buffers,        "adv/buffers")
        _spn(self.spn_avsync,         "adv/avsync")
        _txt(self.txt_vfilter,        "adv/vfilter")
        _txt(self.txt_extra_global,   "adv/extra_global")

    def _save_settings(self):
        """Persist all widget values to QSettings."""
        s = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)

        # CAPTURE tab
        s.setValue("cap/fmt",         self.cmb_v4l2fmt.currentText())
        s.setValue("cap/res",         self.cmb_resolution.currentText())
        s.setValue("cap/fps",         self.cmb_fps.currentText())
        s.setValue("cap/deinterlace", self.chk_deinterlace.isChecked())
        s.setValue("cap/audio_dev",   self.cmb_audio_dev.currentData())
        s.setValue("cap/monitor",     self.chk_monitor.isChecked())
        out = self.txt_output.text().strip()
        if out:
            s.setValue("cap/output_dir", os.path.dirname(out))

        # CODEC tab
        s.setValue("codec/venc",       self.cmb_vcodec.currentData())
        s.setValue("codec/crf",        self.spn_crf.value())
        s.setValue("codec/vbitrate",   self.spn_vbitrate.value())
        s.setValue("codec/preset",     self.cmb_preset.currentText())
        s.setValue("codec/tiles",      self.spn_tiles.value())
        s.setValue("codec/vextra",     self.txt_vextra.text())
        s.setValue("codec/aenc",       self.cmb_acodec.currentData())
        s.setValue("codec/abitrate",   self.spn_abitrate.value())
        s.setValue("codec/opus_vbr",   self.cmb_opus_vbr.currentText())
        s.setValue("codec/compression",self.spn_compression.value())
        s.setValue("codec/aextra",     self.txt_aextra.text())
        s.setValue("codec/container",  self.cmb_container.currentText())

        # VIDEO tab
        s.setValue("video/rotate",     self.cmb_rotate.currentText())
        s.setValue("video/flip_h",     self.chk_flip_h.isChecked())
        s.setValue("video/flip_v",     self.chk_flip_v.isChecked())
        s.setValue("video/zoom",       self.sld_zoom.value())
        s.setValue("video/brightness", self.sld_brightness.value())
        s.setValue("video/contrast",   self.sld_contrast.value())
        s.setValue("video/saturation", self.sld_saturation.value())
        s.setValue("video/gamma",      self.sld_gamma.value())

        # ADVANCED tab
        s.setValue("adv/thread_q",     self.spn_thread_q.value())
        s.setValue("adv/buffers",      self.spn_buffers.value())
        s.setValue("adv/avsync",       self.spn_avsync.value())
        s.setValue("adv/vfilter",      self.txt_vfilter.text())
        s.setValue("adv/extra_global", self.txt_extra_global.text())

        s.sync()

    def _reset_settings(self):
        """Wipe all saved settings and restart defaults."""
        r = QMessageBox.question(self, "Reset Settings",
            "Clear all saved settings and restore defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        QSettings(self._SETTINGS_ORG, self._SETTINGS_APP).clear()
        # Restore coded defaults
        self.cmb_resolution.setCurrentIndex(0)
        self.cmb_fps.setCurrentIndex(0)
        self.chk_deinterlace.setChecked(True)
        self.chk_monitor.setChecked(True)
        self.spn_crf.setValue(28)
        self.spn_vbitrate.setValue(0)
        self.cmb_preset.setCurrentIndex(6)
        self.spn_tiles.setValue(2)
        self.txt_vextra.clear()
        self.spn_abitrate.setValue(128)
        self.cmb_opus_vbr.setCurrentIndex(0)
        self.spn_compression.setValue(10)
        self.txt_aextra.clear()
        self.cmb_container.setCurrentIndex(0)
        self._reset_colour()
        self.cmb_rotate.setCurrentIndex(0)
        self.chk_flip_h.setChecked(False)
        self.chk_flip_v.setChecked(False)
        self.sld_zoom.setValue(100)
        self.spn_thread_q.setValue(512)
        self.spn_buffers.setValue(4)
        self.spn_avsync.setValue(0.0)
        self.txt_vfilter.clear()
        self.txt_extra_global.clear()
        ts = datetime.now().strftime("capture_%Y%m%d_%H%M%S.webm")
        self.txt_output.setText(
            os.path.join(os.path.expanduser("~"), "Videos", ts))
        self._log("[settings] All settings reset to defaults")

    # ─────────────────────────── Devices ─────────────────────

    def _refresh_devices(self):
        """
        Kick off ALL background probing — never blocks the main thread.
        Two parallel workers:
          ProbeWorker    — ffmpeg -encoders + pactl list sources
          DeviceScanWorker — pw-dump + v4l2-ctl format queries
        Both emit signals that are handled on the main thread.
        """
        if (self.scan_worker  and self.scan_worker.isRunning()) or            (self.probe_worker and self.probe_worker.isRunning()):
            return

        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("↻  SCANNING…")
        self.cmb_device.clear()
        self.cmb_device.addItem("  scanning…", None)
        self.cmb_device.setEnabled(False)
        self.cmb_vcodec.clear()
        self.cmb_vcodec.addItem("  probing…")
        self.cmb_acodec.clear()
        self.cmb_acodec.addItem("  probing…")
        self.cmb_audio_dev.clear()
        self.cmb_audio_dev.addItem("  probing…", "default")
        self._device_formats.clear()

        # Worker 1: encoder + ALSA probing
        self.probe_worker = ProbeWorker()
        self.probe_worker.probe_done.connect(
            self._on_probe_done, Qt.ConnectionType.QueuedConnection)
        self.probe_worker.start()

        # Worker 2: PipeWire video device scan
        self.scan_worker = DeviceScanWorker()
        self.scan_worker.scan_progress.connect(
            self.statusBar().showMessage, Qt.ConnectionType.QueuedConnection)
        self.scan_worker.scan_done.connect(
            self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        self.scan_worker.start()

        self.statusBar().showMessage("Scanning devices and probing encoders…")

    def _on_probe_done(self, result: dict):
        """Populate codec combos from ProbeWorker results — main thread only."""
        vencs = result.get("video_encoders", [])
        aencs = result.get("audio_encoders", [])
        adevs = result.get("pw_audio", [])
        self._log(f"[probe] {len(vencs)} video encoders, "
                  f"{len(aencs)} audio encoders, "
                  f"{len(adevs)} audio sources found")
        for name, _ in vencs:
            self._log(f"[venc]  {name}")

        self.cmb_vcodec.clear()
        for name, desc in vencs:
            self.cmb_vcodec.addItem(f"{name}  —  {desc}", name)
        # Auto-select: prefer first vaapi encoder, then first svt/aom av1
        selected = False
        for prefer in ("vaapi", "svtav1", "libaom", "libvpx"):
            for i in range(self.cmb_vcodec.count()):
                if prefer in self.cmb_vcodec.itemText(i).lower():
                    self.cmb_vcodec.setCurrentIndex(i)
                    selected = True
                    break
            if selected:
                break

        self.cmb_acodec.clear()
        for name, desc in aencs:
            self.cmb_acodec.addItem(f"{name}  —  {desc}", name)
        for i in range(self.cmb_acodec.count()):
            if "opus" in self.cmb_acodec.itemText(i).lower():
                self.cmb_acodec.setCurrentIndex(i)
                break

        self.cmb_audio_dev.clear()
        for dev, label in adevs:
            self.cmb_audio_dev.addItem(label, dev)

        # Re-enable refresh only when both workers are done
        if not (self.scan_worker and self.scan_worker.isRunning()):
            self.btn_refresh.setEnabled(True)
            self.btn_refresh.setText("↻  SCAN")

        # Restore saved settings now that combos are populated with real values
        if not hasattr(self, "_settings_loaded"):
            self._settings_loaded = True
            self._load_settings()

    def _on_scan_done(self, results):
        """Called on main thread when DeviceScanWorker finishes."""
        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()

        if not results:
            self.cmb_device.addItem("  no capture devices found", None)
        else:
            for dev in results:
                self.cmb_device.addItem(dev["label"], dev)
                if dev["backend"] == "v4l2":
                    self._device_formats[dev["path"]] = dev["formats"]

        self.cmb_device.blockSignals(False)
        self.cmb_device.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("↻  SCAN")

        n = len(results)
        self.statusBar().showMessage(
            f"Found {n} capture device{'s' if n != 1 else ''}" +
            ("" if n else " — is PipeWire running?"))

        # Re-enable refresh only when both workers are done
        if not (self.probe_worker and self.probe_worker.isRunning()):
            self.btn_refresh.setEnabled(True)
            self.btn_refresh.setText("↻  SCAN")

        self._on_device_changed()

    def _on_device_changed(self):
        dev = self.cmb_device.currentData()
        if not dev:
            return
        self._auto_orient_for_device(dev)
        self.cmb_v4l2fmt.clear()
        if dev["backend"] == "v4l2":
            fmts = dev.get("formats") or []
            if fmts:
                for f in fmts:
                    self.cmb_v4l2fmt.addItem(f)
                # Prefer MJPEG to avoid YUYV USB bandwidth issues
                for prefer in ("MJPEG", "H264"):
                    for i in range(self.cmb_v4l2fmt.count()):
                        if self.cmb_v4l2fmt.itemText(i).upper() == prefer:
                            self.cmb_v4l2fmt.setCurrentIndex(i)
                            break
            else:
                # Formats not yet known — add sensible defaults and probe async
                for f in ["MJPEG", "YUYV", "H264"]:
                    self.cmb_v4l2fmt.addItem(f)
                self._probe_formats_async(dev["path"])
            self.cmb_v4l2fmt.setEnabled(True)
        else:
            # pipewire/libcamera — format negotiated by GStreamer
            self.cmb_v4l2fmt.addItem("(negotiated by PipeWire)")
            self.cmb_v4l2fmt.setEnabled(False)

    def _probe_formats_async(self, dev_path: str):
        """Query v4l2-ctl formats off the main thread, update combo when done."""
        def _run():
            fmts = _v4l2_formats_for(dev_path)
            if fmts:
                # Marshal result back to main thread via a single-shot QTimer
                QTimer.singleShot(0, lambda: self._apply_formats(dev_path, fmts))
        threading.Thread(target=_run, daemon=True).start()

    def _apply_formats(self, dev_path: str, fmts: list):
        """Called on main thread — update format combo if this device is still selected."""
        dev = self.cmb_device.currentData()
        if dev and dev.get("path") == dev_path:
            self.cmb_v4l2fmt.clear()
            for f in fmts:
                self.cmb_v4l2fmt.addItem(f)
            # Prefer MJPEG — avoids YUYV USB bandwidth issues
            for prefer in ("MJPEG", "mjpeg", "H264", "h264"):
                for i in range(self.cmb_v4l2fmt.count()):
                    if self.cmb_v4l2fmt.itemText(i).upper() == prefer.upper():
                        self.cmb_v4l2fmt.setCurrentIndex(i)
                        return

    # ─────────────────────────── Preview ─────────────────────

    def _toggle_preview(self):
        if self._preview_active:
            self._stop_preview()
        else:
            self._start_preview()

    def _start_preview(self):
        dev_info = self.cmb_device.currentData()
        if not dev_info:
            return

        res_text = self.cmb_resolution.currentText().split()[0]
        try:
            w, h = (int(x) for x in res_text.split("x"))
        except ValueError:
            self._log(f"[preview] Cannot parse resolution '{res_text}' — rescan devices")
            return
        res = res_text
        fps  = self.cmb_fps.currentText().split()[0]

        if dev_info["backend"] == "pipewire":
            # libcamera/IPU6 via pipewiresrc — GStreamer pipeline
            pw_id = dev_info.get("pw_node_id")
            if pw_id is None:
                QMessageBox.warning(self, "No PipeWire node ID",
                    "Could not find PipeWire node ID for this device.\n"
                    "Try rescanning devices.")
                return
            flip_method, br, con, sat = self._build_gst_videoflip()
            worker = GstPreviewWorker(pw_id, w, h, fps, flip_method, br, con, sat)
        else:
            # Standard V4L2 — ffmpeg rawvideo pipe
            dev_path = dev_info["path"]
            vf = self._build_vf(for_preview=True)
            v4lfmt_p = self.cmb_v4l2fmt.currentText().strip()
            preview_input = ["-f", "v4l2"]
            if v4lfmt_p and not v4lfmt_p.startswith("("):
                preview_input += ["-input_format", v4lfmt_p.lower()]
            preview_input += ["-video_size", res, "-framerate", fps,
                              "-i", dev_path]
            cmd = ["ffmpeg", "-n", "-hide_banner"] + preview_input
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
            self._log("[preview cmd] " + " ".join(cmd))
            worker = PreviewWorker(cmd, w, h)

        self.preview_worker = worker
        worker.frame_ready.connect(
            self.video_pane.update_frame,
            Qt.ConnectionType.QueuedConnection)
        worker.error.connect(
            lambda e: self._log(f"[preview error] {e}"),
            Qt.ConnectionType.QueuedConnection)
        if hasattr(worker, "stderr_line"):
            worker.stderr_line.connect(
                lambda line: self._log(f"[ffmpeg] {line}"),
                Qt.ConnectionType.QueuedConnection)
        worker.start()

        # Start audio monitor if requested
        self._start_monitor()

        self._preview_active = True
        self.btn_preview.setText("◉  STOP PREVIEW")
        self.statusBar().showMessage("Preview active — [F] or double-click for fullscreen")

    def _start_monitor(self):
        """Start PipeWire audio passthrough if checkbox is checked."""
        if not self.chk_monitor.isChecked():
            return
        pw_src = self.cmb_audio_dev.currentData() or "default"
        self._stop_monitor()   # ensure clean state
        self.monitor_worker = AudioMonitorWorker(pw_src)
        self.monitor_worker.error.connect(
            lambda e: self._log(f"[monitor] {e}"),
            Qt.ConnectionType.QueuedConnection)
        self.monitor_worker.start()

    def _stop_monitor(self):
        """Stop audio passthrough — SIGTERM only, no wait."""
        w = self.monitor_worker
        if w:
            self.monitor_worker = None
            w.stop()   # SIGTERM + scheduled SIGKILL after 1s

    def _stop_preview(self):
        """
        Non-blocking preview teardown.
        Signals the worker to stop (SIGTERM + _running=False), updates UI
        immediately. Never disables buttons and never calls worker.wait().
        """
        self._preview_active = False
        self.btn_preview.setText("◉  PREVIEW")
        self.video_pane.set_active(False)
        self._stop_monitor()   # stop audio monitoring when preview stops

        worker = self.preview_worker
        if not worker:
            self.statusBar().showMessage("Preview stopped")
            return

        try:
            worker.frame_ready.disconnect()
        except Exception:
            pass
        worker.stop()   # SIGTERM + _running=False, non-blocking
        self.preview_worker = None

        self.statusBar().showMessage("Preview stopped")

    # ─────────────────────────── Command builder ─────────────

    def _build_capture_cmd(self):
        dev_info = self.cmb_device.currentData()
        if not dev_info:
            raise ValueError("No device selected")

        if dev_info["backend"] == "pipewire":
            raise ValueError(
                "This device uses libcamera via PipeWire (IPU6).\n\n"
                "Your ffmpeg build does not include a libcamera or PipeWire "
                "input demuxer, so it cannot record from this source.\n\n"
                "Preview works via GStreamer, but recording requires ffmpeg "
                "with --enable-libcamera, or use OBS with the PipeWire input plugin."
            )

        dev_path = dev_info["path"]
        res    = self.cmb_resolution.currentText().split()[0]
        fps    = self.cmb_fps.currentText().split()[0]
        adev   = self.cmb_audio_dev.currentData() or "default"
        venc   = self.cmb_vcodec.currentData() or self.cmb_vcodec.currentText().split()[0]
        aenc   = self.cmb_acodec.currentData() or self.cmb_acodec.currentText().split()[0]
        crf    = self.spn_crf.value()
        vbr    = self.spn_vbitrate.value()
        preset = self.cmb_preset.currentText().split()[0]
        tiles  = self.spn_tiles.value()
        abit   = self.spn_abitrate.value()
        avsync = self.spn_avsync.value()
        vf     = self._build_vf(for_preview=False)
        vextra = self.txt_vextra.text().strip()
        aextra = self.txt_aextra.text().strip()
        eg     = self.txt_extra_global.text().strip()
        output = self.txt_output.text()

        # ── inputs ──────────────────────────────────────────────────────
        v4lfmt = self.cmb_v4l2fmt.currentText().upper()

        cmd = ["ffmpeg", "-n", "-hide_banner"]
        if eg:
            cmd += eg.split()

        # VAAPI device must be declared before all inputs
        if "vaapi" in venc:
            cmd += ["-vaapi_device", "/dev/dri/renderD128"]

        # Video input: -f v4l2 is mandatory for V4L2 devices.
        # -input_format tells the card which compressed/raw pixel format to
        # deliver — MJPEG is strongly preferred for USB capture cards because
        # raw YUYV overflows USB 2.0 bandwidth at anything above 480p and
        # produces the "corrupted data" error.
        v4l_input = ["-f", "v4l2"]
        if v4lfmt:
            v4l_input += ["-input_format", v4lfmt.lower()]
        if "vaapi" in venc:
            v4l_input += ["-hwaccel", "vaapi",
                          "-hwaccel_output_format", "vaapi"]
        v4l_input += ["-i", dev_path]
        cmd += v4l_input

        # Audio input via PulseAudio compat (works on PipeWire via pipewire-pulse)
        if avsync != 0.0:
            cmd += ["-itsoffset", f"{avsync:.3f}"]
        cmd += ["-f", "pulse", "-i", adev]

        # ── video filters ────────────────────────────────────────────────
        if "vaapi" in venc:
            # VAAPI path: software filters run before hwupload.
            # If hwaccel_output_format=vaapi the frame is already on GPU,
            # so we need a hwdownload+convert sandwich around sw filters,
            # OR simply skip hwaccel on input and let the sw filters run
            # then upload. Simplest correct approach: drop hwaccel on input
            # (already decoded in SW by v4l2), apply sw filters, then upload.
            # The hwaccel flags above handle the fast path when there are no
            # sw filters; here we always append the upload step.
            vaapi_vf = []
            if vf:
                vaapi_vf.append(vf)
            vaapi_vf.append("format=nv12,hwupload")
            cmd += ["-vf", ",".join(vaapi_vf)]
        elif vf:
            cmd += ["-vf", vf]

        # ── video codec ──────────────────────────────────────────────────
        cmd += ["-c:v", venc]
        if "vaapi" in venc:
            cmd += ["-qp", str(crf)]
        elif venc not in ("copy",):
            cmd += ["-crf", str(crf)]
            if vbr > 0:
                cmd += ["-b:v", f"{vbr}k"]
            if preset != "auto":
                cmd += ["-cpu-used", preset]
            if tiles > 0:
                cmd += ["-tile-columns", str(tiles)]
        if vextra:
            cmd += vextra.split()

        # ── audio codec ──────────────────────────────────────────────────
        cmd += ["-c:a", aenc, "-b:a", f"{abit}k"]
        if aenc in ("libopus", "libvorbis"):
            cmd += ["-vbr", "on"]
        if aextra:
            cmd += aextra.split()

        cmd.append(output)

        # Audio monitor tee: duplicate audio stream to PipeWire sink
        if self.chk_monitor.isChecked():
            cmd += ["-map", "1:a", "-c:a", "pcm_s16le", "-"]

        # Video monitor tee: scaled-down rawvideo to stdout for VideoPane.
        # Using a separate -map so the main encode vf chain is unaffected.
        # The scale filter also converts yuvj422p → rgb24 safely.
        cmd += [
            "-map", "0:v",
            "-vf", f"scale={_MON_W}:{_MON_H},format=rgb24",
            "-f", "rawvideo",
            "-",
        ]

        return cmd, True   # (cmd, monitor_frames)

    def _update_preview(self):
        try:
            cmd, _ = self._build_capture_cmd()
            self.lbl_preview.setText(" ".join(cmd))
        except Exception as e:
            self.lbl_preview.setText(f"[error] {e}")

    # ─────────────────────────── Record / Stop ───────────────

    def _browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save capture as …", self.txt_output.text(),
            "WebM Video (*.webm);;Matroska (*.mkv);;All Files (*)")
        if path:
            self.txt_output.setText(path)

    def _start_capture(self):
        output = self.txt_output.text().strip()
        if not output:
            QMessageBox.warning(self, "No Output", "Set an output file path first.")
            return
        outdir = os.path.dirname(output)
        if outdir:
            os.makedirs(outdir, exist_ok=True)

        try:
            cmd, monitor_frames = self._build_capture_cmd()
        except ValueError as e:
            QMessageBox.warning(self, "Cannot capture", str(e))
            return
        self._log(f"\n──── CAPTURE  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ────")

        # Stop preview worker to free the V4L2 device, but keep
        # _preview_active=True so the pane stays alive — the capture
        # worker feeds it frames via its own rawvideo tee output.
        if self.preview_worker:
            try: self.preview_worker.frame_ready.disconnect()
            except Exception: pass
            self.preview_worker.stop()
            self.preview_worker = None

        self.capture_worker = CaptureWorker(cmd, monitor_frames)
        # QueuedConnection: all signal deliveries happen on the main thread
        self.capture_worker.log_line.connect(
            self._log, Qt.ConnectionType.QueuedConnection)
        self.capture_worker.time_tick.connect(
            self._on_time, Qt.ConnectionType.QueuedConnection)
        self.capture_worker.finished.connect(
            self._on_capture_finished, Qt.ConnectionType.QueuedConnection)
        if monitor_frames:
            self.capture_worker.frame_ready.connect(
                self.video_pane.update_frame,
                Qt.ConnectionType.QueuedConnection)
        self.capture_worker.start()

        # Stop the preview audio monitor — the capture ffmpeg command
        # handles audio monitoring itself via its own PipeWire output tee
        self._stop_monitor()

        self.btn_record.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_status.setText("● RECORDING")
        self.lbl_status.setStyleSheet(
            f"color:{REC_RED};font-size:11px;letter-spacing:2px;font-weight:bold;")
        self.video_pane.set_recording(True)
        self.statusBar().showMessage(f"Capturing → {output}")

    def _stop_capture(self):
        self.btn_stop.setEnabled(False)
        if self.capture_worker:
            self.capture_worker.stop()

    def _on_capture_finished(self, code):
        self.btn_record.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText("IDLE")
        self.lbl_status.setStyleSheet(
            f"color:{TEXT_SEC};font-size:11px;letter-spacing:2px;")
        self.video_pane.set_recording(False)
        # Restart preview worker if it was active before recording started.
        # _start_preview sets _preview_active=True internally, so clear it
        # first to avoid the guard inside _toggle_preview misfiring.
        if self._preview_active:
            self._preview_active = False
            self._start_preview()
        ok   = code == 0
        msg  = "Capture finished (OK)" if ok else f"ffmpeg exit code {code}"
        tag  = "OK" if ok else "WARN"
        self._log(f"[{tag}] {msg}")
        self.statusBar().showMessage(msg)

    def _on_time(self, t: str):
        t8 = t[:8]
        self.lbl_timer.setText(t8)
        self.video_pane.set_elapsed(t8)

    def _log(self, line: str):
        self.log_view.append(line)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Clean shutdown ────────────────────────────────────────

    def closeEvent(self, ev):
        self._save_settings()
        # Terminate all child processes immediately (non-blocking)
        if self.capture_worker:
            self.capture_worker.stop()
        if self.preview_worker:
            self.preview_worker.stop()
        if self.monitor_worker:
            self.monitor_worker.stop()
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.terminate()
        if self.probe_worker and self.probe_worker.isRunning():
            self.probe_worker.terminate()
        # No wait() calls — SIGTERM+SIGKILL is sufficient.
        # OS reaps all child processes when the parent exits.
        super().closeEvent(ev)

# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Video Capture")
    win = VideoCapture()
    win.show()
    sys.exit(app.exec())
