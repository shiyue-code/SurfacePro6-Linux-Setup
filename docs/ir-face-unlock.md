# Surface Pro 6 — IR Face Unlock (Windows Hello style)

Linux implementation of face unlock using the OV7251 IR sensor behind the Intel IPU3,
upstream howdy-next (C++) for recognition, and v4l2loopback as the camera abstraction.

## Data flow

```
ov7251 (IR sensor, /dev/media0)
   │ media-ctl: ov7251:0 → ipu3-csi2 2:0  (Y10_1X10/640x480)
   ▼
/dev/video12 (ipu3-cio2 2, pixel format "ip3y", 10-bit packed grey)
   │  ir_bridge (C): unpack ip3y → 8-bit GREY, rotate, I2C writes 0x3005=0x08
   │                 to strobe the IR emitter on i2c-3
   ▼
/dev/video42 (v4l2loopback, GREY 480x640, keep_format=1)
   │  consumers: howdy-compare (PAM) / face-enroll.py (GUI) / cv2
   ▼
howdy-next 3.4.2 → pam_howdy.so → YuNet detect + SFace match → PAM auth
```

## Components

| File | Role |
|---|---|
| `/usr/local/bin/ir_bridge` | C producer: V4L2 capture → ip3y decode → loopback write + LED trigger. `IR_BRIDGE_LIFETIME_SEC` env sets self-timeout (default 15s, app uses 28800s). `PR_SET_PDEATHSIG` kills it when the parent dies. |
| `/usr/local/bin/ir_bridge_run.sh` | Launcher: re-links media graph every run, sets ALS-adaptive exposure/gain, resolves the cio2 node dynamically, `exec ir_bridge`. |
| `/usr/local/bin/setup_ipu3.sh` | One-shot media-graph setup used at boot (baseline exposure/gain). |
| `/usr/local/libexec/howdy/howdy-compare` | Wrapper (bash): spawns `ir_bridge_run.sh` in background, execs `howdy-compare.real`, kills bridge on exit. |
| `/usr/local/libexec/howdy/howdy-compare.real` | Real howdy-next compare binary (spawned by pam_howdy). |
| `/usr/lib/security/pam_howdy.so` | Native PAM module (copied from `/usr/local/lib/security/`). |
| `/usr/local/etc/howdy/config.ini` | `device_path=/dev/video42`, `detection_notice=true`. Must be world-readable for lock-screen/polkit (unprivileged PAM callers). |
| `/usr/local/etc/howdy/models/*.dat` | Face models (needs o+r for unprivileged PAM callers). |
| `/usr/share/pam-configs/howdy` | pam-auth-update entry → `auth [success=end default=ignore] pam_howdy.so` in common-auth. |
| `/etc/systemd/system/surface_ir_bridge.service` | Boot setup: modprobe v4l2loopback + setup_ipu3.sh. |
| `/etc/modprobe.d/v4l2loopback.conf` | `video_nr=42 card_label="Surface IR Camera" keep_format=1` |
| `/etc/udev/rules.d/99-surface-ir-i2c.rules` | `i2c-3 MODE=0666` (PAM children run as user sy; group propagation unreliable). |
| `~/.local/bin/face-enroll.py` | GTK4 manager UI: preview + face guide + enroll + model list/remove. Worker-thread capture, watchdog respawn, wedge self-heal (60 empty reads → full reset), releases cap during `howdy add`. |

## Hardware notes

- OV7251 is NOT libcamera-compatible (no IPU3 format); raw path via media-ctl only.
- IR emitter: no kernel control node → direct I2C write `0x3005=0x08` (strobe).
- Sensor frame: 640x480 Y10 packed, 832 B/line → decoded to 480x640 GREY (rotated).
- `/dev/video*` numbering changes per boot — always resolve via `media-ctl -e "ipu3-cio2 2"`.
- libcamera/PipeWire probing can reset the media graph — that's why the link is
  re-established in `ir_bridge_run.sh` on every auth, not only at boot.

## Auth coverage

common-auth → sudo, su, polkit dialogs, GDM login, GNOME lock screen.
Password fallback is always preserved (`[success=end default=ignore]`).

## Known quirks

- v4l2loopback consumer wedge: opening a capture fd while the producer is not yet
  streaming (or re-opening at a bad moment) can yield permanent empty reads.
  Mitigations: open only after bridge reports LED on + 2.5s, plus self-heal reset.
- keep_format=1 is required so the GREY format survives producer restarts.
- exclusive_caps is left OFF — role assignment was ambiguous for O_RDWR opens.
