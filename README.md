# Phone Webcam

Use a phone's camera as a webcam on a Linux PC, over Wi-Fi, with nothing to install on the phone.

The PC runs a small HTTPS page. Open it in the phone's browser, tap **Broadcast**, and the
picture shows up as a webcam called **PhoneCam** that Discord, Vesktop, OBS, and browsers
can select.

- Works in any modern phone browser (H.264 where supported, JPEG or lossless PNG otherwise)
- Pick the phone camera, resolution, frame rate, and quality from the phone
- PhoneCam takes the shape of the phone's picture, upright or sideways, and never adds black bars
- Live stats on the phone: camera mode, frames per second on both ends, bitrate, encode time, delay
- Low delay: frames the network can't keep up with are skipped instead of queuing up

## Requirements

Developed on Fedora with KDE (Wayland). Other distributions should work with the same pieces.

- Python 3.9 or newer
- GStreamer with these plugin sets, plus OpenSSL:

  ```
  sudo dnf install gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
      gstreamer1-plugins-bad-free gstreamer1-plugin-libav pipewire-gstreamer \
      v4l-utils openssl
  ```

  `gstreamer1-plugin-libav` provides the H.264 decoder. Without it, pick JPEG on the phone.

- [v4l2loopback](https://github.com/umlaeute/v4l2loopback), which creates the virtual webcam.
  On Fedora it comes from RPM Fusion:

  ```
  sudo dnf install akmod-v4l2loopback v4l2loopback kernel-devel
  ```

- The phone and the PC on the same network, and TCP port 8443 reachable on the PC
  (on Fedora: `sudo firewall-cmd --add-port=8443/tcp`, add `--permanent` to keep it).

## Setup

Run the setup script once as root. It loads v4l2loopback and creates the PhoneCam device:

```
sudo ./enable-v4l2-webcam.sh
```

What it does:

1. With Secure Boot on, the kernel only loads signed modules. The script signs v4l2loopback
   with the module signing key that DKMS or akmods already enrolled on the machine
   (`/var/lib/dkms/mok.key` or `/etc/pki/akmods/private/private_key.priv`) and installs it.
2. Loads the module and creates the PhoneCam device at `/dev/video10`.
3. Adds a udev rule so your user can open PhoneCam without root, and adds a user to the
   `video` group. The username is currently written into the script; change it to yours.

Stop `phonecam.py` and any app using the camera before running it.

> **Known limitation:** the device is created by hand, so it does not survive a reboot yet.
> Run the script again after restarting the PC.

## Use

```
python3 phonecam.py
```

1. The terminal prints a link like `https://192.168.1.20:8443/#123456`. Open it on the phone.
2. The browser warns about the certificate, because it is self-signed. Continue anyway; this
   is what lets the browser use the camera over your local network.
3. Allow camera access, then tap **Broadcast**.
4. In Discord, Vesktop, OBS, or a browser, select the camera **PhoneCam**.

Start the script before opening Discord or Vesktop. If the app was already open before
PhoneCam existed, quit it fully (including the tray icon) and reopen it.

Stop with Ctrl+C. Press it twice to force quit.

### Options

| Option | Default | Meaning |
|---|---|---|
| `--port` | `8443` | Port for the phone page |
| `--size WIDTHxHEIGHT` | last shape used, else `1280x720` | PhoneCam's starting size; it then follows the phone |
| `--fps` | `30` | Frame rate PhoneCam advertises to apps |

### The phone page

| Control | What it does |
|---|---|
| Camera | Front, back, and any extra lenses the phone exposes |
| Format | **H.264**: hardware video encoding, best frame rate. **JPEG**: works in every browser. **PNG**: lossless, but frames are 1–3 MB, so expect a few fps over Wi-Fi |
| Resolution | Long edge of the picture sent to the PC |
| Frame rate | Target fps. Many phones cap the camera at 30 |
| Quality | H.264 bitrate or JPEG quality |

Settings can be changed while broadcasting.

| Stat | Meaning |
|---|---|
| Camera | The mode the phone camera is actually running in |
| Sending | Size and format of the frames sent |
| Phone fps | Frames sent per second, and how many were skipped |
| PC fps | Frames per second reaching PhoneCam |
| Bitrate / Avg frame | Network use |
| Encode | Time the phone spends compressing a frame |
| Frame delay | Time from capture to the PC confirming it received the frame |

If **Phone fps** is low and **Encode** is high, the phone is the limit: lower the resolution
or use H.264. If frames are being skipped while encode time is fine, the Wi-Fi is the limit.

## Portrait and landscape

PhoneCam takes the shape of the phone's picture: hold the phone upright and PhoneCam is
upright, turn it sideways and PhoneCam turns too.

v4l2loopback cannot change a camera's shape while an app is reading it. If you rotate the
phone while Discord or OBS has PhoneCam open, the picture is cropped to fill the current
shape instead of getting black bars, and the phone says so. Turn the camera off and on in that
app, and PhoneCam switches to the new shape within about a second.

## How it works

```
phone browser                         PC: phonecam.py
camera → canvas → H.264/JPEG/PNG ──WebSocket over HTTPS──▶ gst-launch decoder
                                                            (crop + scale to YUY2)
                                                                    │
                                     Discord / OBS ◀── /dev/video10 (v4l2loopback)
```

- `phonecam.py` is a single file using only the Python standard library. It serves the phone
  page, receives frames over a WebSocket, and starts one `gst-launch-1.0` process per stream
  to decode them.
- Decoded frames are written straight to the v4l2loopback device. When no phone is
  broadcasting, PhoneCam shows black.
- The PC acknowledges every frame. The phone stops sending while too many frames are
  unacknowledged, so the delay stays low instead of building up.
- Without v4l2loopback, the script falls back to a PipeWire camera. OBS can use that, but
  Discord and Vesktop show it as black.

Generated at runtime and kept out of git: `cert.pem` and `key.pem` (the page's self-signed
certificate, created for the PC's current IP addresses) and `.last-size`.

## Troubleshooting

**Discord or Vesktop doesn't list PhoneCam.** Check that `/dev/video10` exists (`v4l2-ctl --list-devices`);
if not, run the setup script again. Then fully quit the app and reopen it with `phonecam.py` running.

**"Could not open the virtual camera".** Another copy of `phonecam.py` is still running.
Stop it and try again.

**"No V4L2 loopback device found".** The setup script hasn't been run since the last reboot.

**Vesktop asks you to sign in every time.** An old Vesktop process is still running in the
background and holding its login data. Run `flatpak kill dev.vencord.Vesktop`, then open it again.

**The phone says the camera is blocked.** Open the `https://` link (not `http://`), accept
the certificate warning, and allow camera access in the browser's site settings.
