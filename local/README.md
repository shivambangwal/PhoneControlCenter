# 20-Phone Control Center (macOS prototype)

A Tkinter desktop dashboard for Android phones connected over USB. It shows a responsive phone grid, live H.264 previews with ADB screenshot fallback, and supports selecting phones, waking their screens, launching Facebook, Instagram, X, YouTube, Snapchat, Google Drive, or Play Store, running a manual timed app session, and mirroring supported input.

## Requirements

- macOS with Python 3 and Tkinter.
- Android Platform Tools (`adb`).
- Optional: FFmpeg for smoother live previews; without it the app uses periodic ADB screenshots.
- Optional: Pillow for image decoding and resizing.

Install command-line tools with Homebrew:

```bash
brew install android-platform-tools ffmpeg
```

## Run

From Terminal:

```bash
cd ~/Downloads/PhoneControlCenter/local
/opt/homebrew/bin/python3.13 phone_control_center.py
```

## First-time Android setup

1. Enable Developer options and USB debugging on each Android phone.
2. Connect each phone with a data-capable USB cable.
3. Unlock it and approve the USB debugging authorization prompt.
4. Confirm it appears in `adb devices`.
5. In the app, click **Refresh devices**.

## Android app launch

Select the phones in the device cards, then click an app button to open Facebook, Instagram, X, YouTube, Snapchat, Google Drive, or Play Store on those phones. The app must be installed on each selected phone. Play Store is the Android app store.

## Manual app session

Select the Android phones, then start **Power Boost · manual session** to open Facebook, Instagram, and YouTube in sequence, with a three-minute timer for each app. The session uses the phones selected when it starts. **Next** becomes available when the timer reaches zero; the timer never advances automatically. Pause, resume, or stop the session at any time. Scrolling and all other phone input remain manual, and the current app stays open when the timer or session ends.

## Notes

This is a source-based working prototype, not a signed/notarized `.app` bundle. Run it on the Mac where the phones and required command-line tools are installed.
