#!/usr/bin/env bash
# build-srpm.sh — assemble source tarball and SRPM for COPR submission
# Usage: ./build-srpm.sh [version]
#
# Prerequisites:
#   dnf install rpm-build rpmdevtools copr-cli
#   copr-cli login   (creates ~/.config/copr)

set -euo pipefail

VERSION="${1:-0.1.0}"
NAME="video-capture"
PKGDIR="${NAME}-${VERSION}"

echo "==> Building ${NAME}-${VERSION}"

# ── 1. Set up rpmbuild tree ──────────────────────────────────────────────────
rpmdev-setuptree   # creates ~/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# ── 2. Assemble source tree ──────────────────────────────────────────────────
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

mkdir -p "${TMPDIR}/${PKGDIR}"

cp video_capture.py        "${TMPDIR}/${PKGDIR}/"
cp video-capture.desktop   "${TMPDIR}/${PKGDIR}/"
cp video-capture.spec      "${TMPDIR}/${PKGDIR}/"

# Placeholder icon — replace with a real 256×256 PNG before publishing
if [ -f video-capture.png ]; then
    cp video-capture.png "${TMPDIR}/${PKGDIR}/"
else
    echo "WARNING: video-capture.png not found — creating placeholder"
    # Requires ImageMagick; skip gracefully if absent
    if command -v convert &>/dev/null; then
        convert -size 256x256 xc:'#0e0e0f' \
            -fill '#e8a020' -pointsize 48 -gravity center \
            -annotate 0 'CAP' \
            "${TMPDIR}/${PKGDIR}/video-capture.png"
    else
        # Tiny valid PNG (1×1 black pixel) as absolute fallback
        printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82' \
            > "${TMPDIR}/${PKGDIR}/video-capture.png"
    fi
fi

# LICENSE and README (create stubs if absent)
if [ ! -f LICENSE ]; then
    echo "MIT License — $(date +%Y) — see source for full text" > "${TMPDIR}/${PKGDIR}/LICENSE"
else
    cp LICENSE "${TMPDIR}/${PKGDIR}/"
fi

if [ ! -f README.md ]; then
    echo "# video-capture" > "${TMPDIR}/${PKGDIR}/README.md"
    echo "V4L2 analogue video capture tool. See video_capture.py for details." >> "${TMPDIR}/${PKGDIR}/README.md"
else
    cp README.md "${TMPDIR}/${PKGDIR}/"
fi

# ── 3. Create source tarball ─────────────────────────────────────────────────
tar -czf ~/rpmbuild/SOURCES/${NAME}-${VERSION}.tar.gz \
    -C "${TMPDIR}" "${PKGDIR}"
echo "==> Source tarball: ~/rpmbuild/SOURCES/${NAME}-${VERSION}.tar.gz"

# ── 4. Copy spec ─────────────────────────────────────────────────────────────
cp video-capture.spec ~/rpmbuild/SPECS/

# ── 5. Build SRPM ────────────────────────────────────────────────────────────
rpmbuild -bs ~/rpmbuild/SPECS/video-capture.spec
SRPM=$(ls ~/rpmbuild/SRPMS/${NAME}-${VERSION}-*.src.rpm 2>/dev/null | head -1)
echo "==> SRPM: ${SRPM}"

# ── 6. Optionally submit to COPR ─────────────────────────────────────────────
if command -v copr-cli &>/dev/null && [ -f ~/.config/copr ]; then
    # Edit COPR_PROJECT to match your copr username/project
    COPR_PROJECT="${COPR_PROJECT:-yourusername/video-capture}"
    echo "==> Submitting to COPR: ${COPR_PROJECT}"
    copr-cli build "${COPR_PROJECT}" "${SRPM}"
else
    echo ""
    echo "==> SRPM ready. To submit manually:"
    echo "    copr-cli build yourusername/video-capture ${SRPM}"
    echo ""
    echo "    Or upload via https://copr.fedorainfracloud.org"
fi
