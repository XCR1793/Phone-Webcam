#!/usr/bin/env bash
# Sign + load v4l2loopback with SEPARATE output/capture nodes.
# Discord only lists capture devices. A single loopback node stuck as
# "Video Output" while PhoneCam writes, so Vesktop saw nothing.
set -euo pipefail

KVER="$(uname -r)"
MODULE_DIR="/lib/modules/${KVER}/extra/v4l2loopback"
MODULE_XZ="${MODULE_DIR}/v4l2loopback.ko.xz"
SIGN_FILE="/usr/src/kernels/${KVER}/scripts/sign-file"
RPM_CACHE="/var/cache/akmods/v4l2loopback/kmod-v4l2loopback-${KVER}-0.15.4-2.fc45.x86_64.rpm"
WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

if [[ ! -x "$SIGN_FILE" ]]; then
  echo "kernel sign-file not found at $SIGN_FILE" >&2
  exit 1
fi

if [[ -r /var/lib/dkms/mok.key && -r /var/lib/dkms/mok.pub ]]; then
  KEY=/var/lib/dkms/mok.key
  CERT=/var/lib/dkms/mok.pub
elif [[ -r /etc/pki/akmods/private/private_key.priv && -r /etc/pki/akmods/certs/public_key.der ]]; then
  KEY=/etc/pki/akmods/private/private_key.priv
  CERT=/etc/pki/akmods/certs/public_key.der
else
  echo "No enrolled module signing key found." >&2
  exit 1
fi

if [[ -f "$RPM_CACHE" ]]; then
  ( cd "$WORKDIR" && rpm2cpio "$RPM_CACHE" | cpio -idm >/dev/null 2>&1 )
  SRC_XZ="$(find "$WORKDIR" -name 'v4l2loopback.ko.xz' | head -n1)"
  xz -dc "$SRC_XZ" > "$WORKDIR/v4l2loopback.ko"
elif [[ -f "$MODULE_XZ" ]]; then
  xz -dc "$MODULE_XZ" > "$WORKDIR/v4l2loopback.ko"
else
  echo "v4l2loopback module not found" >&2
  exit 1
fi

"$SIGN_FILE" sha256 "$KEY" "$CERT" "$WORKDIR/v4l2loopback.ko"
xz -f -C crc32 -c "$WORKDIR/v4l2loopback.ko" > "$WORKDIR/v4l2loopback.ko.xz"
mkdir -p "$MODULE_DIR"
install -m 644 "$WORKDIR/v4l2loopback.ko.xz" "$MODULE_XZ"
depmod -a

# Override the distro "OBS Virtual Camera" defaults. devices=0 = create none at
# load time; we add a split output/capture pair with v4l2loopback-ctl.
cat > /etc/modprobe.d/zz-phonecam.conf <<'EOF'
# PhoneCam: dynamic devices via v4l2loopback-ctl
options v4l2loopback devices=0
EOF
rm -f /etc/modprobe.d/phonecam.conf

if fuser /dev/video0 >/dev/null 2>&1 || fuser /dev/video10 >/dev/null 2>&1 || fuser /dev/video11 >/dev/null 2>&1; then
  echo "PhoneCam devices are busy. Stop phonecam.py (Ctrl+C) first, then re-run this script." >&2
  fuser -v /dev/video0 /dev/video10 /dev/video11 2>&1 || true
  exit 1
fi

modprobe -r v4l2loopback 2>/dev/null || true
modprobe v4l2loopback devices=0

# Split pair: phonecam.py WRITES to video10, Discord READS video11.
v4l2loopback-ctl delete /dev/video0 /dev/video10 /dev/video11 2>/dev/null || true
v4l2loopback-ctl add -n PhoneCam -x 1 -v /dev/video10 /dev/video11

cat > /etc/udev/rules.d/99-phonecam.rules <<'EOF'
KERNEL=="video[0-9]*", ATTR{name}=="PhoneCam*", GROUP="video", MODE="0660", TAG+="uaccess"
KERNEL=="v4l2loopback", GROUP="video", MODE="0660", TAG+="uaccess"
EOF
udevadm control --reload
udevadm trigger --subsystem-match=video4linux 2>/dev/null || true
if ! id -nG ovo | grep -qw video; then
  usermod -aG video ovo || true
  echo "Added ovo to the video group (log out/in once if needed)."
fi

echo
echo "Module:"
modinfo v4l2loopback | grep -E '^(filename|signer|version)' || true
echo
echo "Devices (Discord needs the CAPTURE one):"
for d in /dev/video10 /dev/video11 /dev/video0 /dev/video1; do
  [[ -e "$d" ]] || continue
  echo "--- $d ($(cat /sys/class/video4linux/$(basename "$d")/name 2>/dev/null))"
  v4l2-ctl -d "$d" --all 2>/dev/null | grep -E 'Card type|Device Caps|Video Capture|Video Output|Width/Height|Pixel Format' | sed 's/^/  /'
done
echo
v4l2loopback-ctl list || true
echo
echo "Next:"
echo "  python3 $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/phonecam.py"
echo "  Open the printed link on the phone, tap Broadcast, then select PhoneCam in your app."
