#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build a Dream 2.2 DRM console receiver that actually produces AUDIO.
#
# Written 2026-07-29 for the radiotuna DRM campaign (task #34).  Runs on
# Ubuntu (tested: WSL2 Ubuntu 24.04 "noble" on the Windows lab box); it never
# touches an SDR - it decodes archived 48 kHz / 12 kHz-IF wav files.
#
# Credits / licensing:
#   Dream AM/DRM Receiver - GPL-2.0-or-later, Volker Fischer, Alexander
#     Kurpiers, Julian Cable et al., TU Darmstadt / BBC / Fraunhofer IIS.
#     Source: sourceforge.net/projects/drm  (dream_2.2.orig.tar.gz)
#   faad2 (libfaad_drm) - GPL-2.0, knik0/faad2.  Built with DRM_SUPPORT; this
#     is what actually decodes DRM30's ER-AAC-SCAL legacy AAC.
#   fdk-aac - Fraunhofer FDK AAC Codec Library, "FDK AAC" licence (see
#     mstorsjo/fdk-aac NOTICE); used here only as the xHE-AAC/USAC decoder.
#     NO PATENT LICENCE is granted by that licence - bench use only.
#   xHE-AAC super-frame fixes follow the "14-line fix for xHE-AAC decoding"
#     thread on the Dream forum:
#     sourceforge.net/p/drm/discussion/general/thread/01c6e64c3b/
#
# Everything the patch does, and WHY, is in dream22_drm_audio.patch next to
# this script, and in drm_day_log.md.
# ---------------------------------------------------------------------------
set -euo pipefail

SRC=${SRC:-$HOME/dreambuild}          # Dream source + build tree
FAAD=${FAAD:-$HOME/faad2}             # faad2 checkout
FDK=${FDK:-$HOME/fdk202}              # fdk-aac 2.0.2 checkout
LAB=${LAB:-/mnt/z/src/gr-radiotuna/lab}
TARBALL=${TARBALL:-$HOME/dream_2.2.orig.tar.gz}

echo "== 1. dependencies"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential cmake git qtbase5-dev qt5-qmake \
    libfftw3-dev libsndfile1-dev libpcap-dev zlib1g-dev \
    libopus-dev libspeexdsp-dev portaudio19-dev

echo "== 2. Dream 2.2 source"
# NB: use the ORIGINAL TARBALL, not the 2019-04-24 win32 binary.  The binary
# predates the fix for "DecOpen sample rate was zero" and dies on any service
# whose extSamplingRate is 0 (i.e. every legacy AAC service without SBR).
[ -f "$TARBALL" ] || curl -sSL -o "$TARBALL" \
  "https://sourceforge.net/projects/drm/files/dream/2.2/dream_2.2.orig.tar.gz/download"
rm -rf "$SRC"; mkdir -p "$SRC"
tar xzf "$TARBALL" -C "$(dirname "$SRC")" --strip-components=1 -C "$SRC" 2>/dev/null || {
    tmp=$(mktemp -d); tar xzf "$TARBALL" -C "$tmp"
    mv "$tmp"/dream-2.2/* "$SRC"/; rm -rf "$tmp"; }
rm -rf "$SRC/.svn"

echo "== 3. apply the DRM-audio patch"
patch -p1 -d "$SRC" < "$LAB/dream22_drm_audio.patch"

echo "== 4. faad2 with DRM_SUPPORT  ->  libfaad_drm.so.2"
[ -d "$FAAD" ] || git clone --depth 1 https://github.com/knik0/faad2 "$FAAD"
cmake -S "$FAAD" -B "$FAAD/build" -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON -DFAAD_BUILD_CLI=OFF
cmake --build "$FAAD/build" -j"$(nproc)" --target faad_drm

echo "== 5. fdk-aac 2.0.2 from source  ->  libfdk-aac.so.2"
# The DISTRO package (Ubuntu libfdk-aac2 2.0.2-3) SEGFAULTS inside
# aacDecoder_ConfigRaw() on every DRM xHE-AAC config.  A stock upstream 2.0.2
# built here does not.  Do not use the distro one.
[ -d "$FDK" ] || { curl -sSL -o /tmp/fdk202.tgz \
    https://github.com/mstorsjo/fdk-aac/archive/refs/tags/v2.0.2.tar.gz
    mkdir -p "$FDK"; tar xzf /tmp/fdk202.tgz -C "$FDK" --strip-components=1; }
cmake -S "$FDK" -B "$FDK/b" -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_SHARED_LIBS=ON
cmake --build "$FDK/b" -j"$(nproc)"

echo "== 6. Dream console"
cd "$SRC"
/usr/lib/qt5/bin/qmake dream.pro CONFIG+=console CONFIG+=fdk-aac DEFINES+=HAVE_USAC
make -j"$(nproc)"
cp "$FAAD/build"/libfaad_drm.so.2.* .
ln -sf libfaad_drm.so.2.* libfaad_drm.so.2

echo "== 7. a null ALSA device, so PortAudio has somewhere to open"
[ -f ~/.asoundrc ] || printf 'pcm.!default { type null }\nctl.!default { type null }\n' > ~/.asoundrc

cat <<EOF

Built: $SRC/dream

Decode an archived 48 kHz mono 12 kHz-IF catch (real time - a 300 s catch
takes 300 s), and note the SDC station label printed on stderr:

  cd $SRC
  LD_LIBRARY_PATH=$FDK/b:. ./dream \\
      -f $LAB/drm_live_catch_17680_kiwi_if12.wav \\
      -w /tmp/out.wav < /dev/null

Dream leaves the RIFF/data length fields at 0 when it is stopped, so run the
result through drm_audio_audit.py (same directory) which repairs the header
and reports duration / RMS / speech-likeness.
EOF
