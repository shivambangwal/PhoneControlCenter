# Phone Control Center Web App

This browser dashboard controls Android phones connected by USB to a Mac or Windows PC. Hostinger serves the static page; the computer with the phones runs the local bridge and ADB.

## Features

- One responsive grid of connected Android screens.
- Live H.264 previews with FFmpeg, or ADB screenshot fallback.
- Per-phone selection, keyboard target, mirrored taps/swipes/keys, wake, one-click launch buttons for Facebook, Instagram, X, YouTube, Snapchat, Google Drive, and Play Store, and a manual three-minute Facebook/Instagram/YouTube session.
- Local bridge setup for Mac and Windows.

## Upload the dashboard to Hostinger

1. In Hostinger File Manager, open `public_html` (or a subfolder such as `public_html/phones`).
2. Upload `public_html/index.html` from this project, or upload `Phone-Control-Center-Hostinger-Upload.zip` and extract it there.
3. Enable HTTPS for the domain. The bridge config needs the site origin only, such as `https://phones.example.com`; leave off any path and trailing slash.

Do not upload `device_bridge/bridge.json`, the bridge token, or the local bridge scripts to Hostinger.

## Connect from a Mac

Keep the Mac and Android phones together, and open the Hostinger page in a browser on that Mac. The bridge only listens on `127.0.0.1`.

```bash
brew install android-platform-tools ffmpeg
cd ~/Downloads/PhoneControlCenter/webapp/device_bridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python bridge.py --init --origin https://phones.example.com
python bridge.py
```

Replace the example origin with the exact HTTPS origin of your Hostinger page. Save the token printed by `--init`. In the page, paste the token and click **Connect to this computer**. If the browser asks to allow local network access, allow it.

## Connect from a Windows PC

The Windows PC must run the bridge and have the Android phones attached by USB. Open the Hostinger page in a browser on that same Windows PC; the page connects to that computer's loopback bridge.

1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/windows/).
2. Install the latest [Android SDK Platform-Tools for Windows](https://developer.android.com/tools/releases/platform-tools), then add its `platform-tools` folder to `PATH`.
3. Install the USB driver for each phone maker. Google phones use the [Google USB Driver](https://developer.android.com/studio/run/win-usb); other manufacturers provide their own drivers ([OEM driver list](https://developer.android.com/studio/run/oem-usb)).
4. Extract the project bundle and double-click `device_bridge/setup_windows.bat`. Enter the Hostinger HTTPS origin when prompted. This creates a private Python environment, installs the bridge packages, and creates the local token config.
5. Double-click `device_bridge/run_windows.bat` and leave its console open.
6. On each Android phone, enable USB debugging, connect it, unlock it, and approve the computer's debugging prompt. In the browser on Windows, open the Hostinger page, paste the token, and click **Connect to this computer**.

FFmpeg is optional; without it, Android preview uses periodic ADB screenshots. Installing FFmpeg and adding it to `PATH` enables the smoother live H.264 path. The bridge stays bound to `127.0.0.1`; the dashboard is designed to be opened on the same computer as the USB phones.

## Open apps

Select the Android phones in the device cards, then click an app button to open Facebook, Instagram, X, YouTube, Snapchat, Google Drive, or Play Store on the selected phones. The app must be installed on each phone. Play Store is the Android app store.

## Manual app session

Select Android phones and start **Power Boost · manual session** to open Facebook, Instagram, and YouTube in sequence, with a three-minute timer for each app. The session uses the phones selected when it starts. **Next** becomes available when the timer reaches zero; the timer never advances automatically. Pause, resume, or stop the session at any time. Scrolling and all other phone input remain manual, and the current app stays open when the timer or session ends.

## Safety and scope

- The dashboard sends phone commands only after the local bridge token is accepted.
- Keep `bridge.json` private and never upload it to Hostinger.
- The bridge binds only to loopback. A browser on another computer cannot control phones attached to this one.
