Name:           video-capture
Version:        0.1.0
Release:        1%{?dist}
Summary:        V4L2 analogue video capture tool with live preview and VAAPI encoding

License:        MIT
URL:            https://github.com/yourusername/video-capture
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel

# Runtime
Requires:       python3
Requires:       python3-pyqt6
Requires:       ffmpeg
Requires:       gstreamer1-plugins-good
Requires:       gstreamer1-plugins-bad-free
Requires:       pipewire
Requires:       pipewire-pulseaudio
Requires:       v4l-utils

# Optional but strongly recommended for VAAPI encoding on Intel
Recommends:     intel-media-driver
Recommends:     libva-utils

%description
Video Capture is a single-file PyQt6 application for digitising analogue video sources and
other analogue video sources via V4L2 USB capture cards.

Features:
  - Live preview via ffmpeg (V4L2) or GStreamer (libcamera/PipeWire)
  - Live preview continues during recording via a rawvideo tee
  - VAAPI hardware encoding (hevc_vaapi, h264_vaapi) on Intel/AMD/Nvidia
  - Software encoding fallback (libaom-av1, libsvtav1, libx265, libx264)
  - PipeWire audio capture and real-time monitoring
  - Deinterlace, rotate, flip, zoom, colour correction filters
  - All settings persisted via QSettings


%prep
%autosetup


%build
# Pure Python — nothing to build


%install
install -Dm 0755 %{_builddir}/%{name}-%{version}/video_capture.py \
    %{buildroot}%{_bindir}/video-capture

install -Dm 0644 %{_builddir}/%{name}-%{version}/video-capture.desktop \
    %{buildroot}%{_datadir}/applications/video-capture.desktop

install -Dm 0644 %{_builddir}/%{name}-%{version}/video-capture.png \
    %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/video-capture.png


%files
%license LICENSE
%doc README.md
%{_bindir}/video-capture
%{_datadir}/applications/video-capture.desktop
%{_datadir}/icons/hicolor/256x256/apps/video-capture.png


%changelog
* Sun Mar 08 2026 Your Name <you@example.com> - 0.1.0-1
- Initial package
