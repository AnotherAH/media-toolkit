<p align="center">
  <img src="docs/images/icon.png" width="88" alt="">
</p>

<h1 align="center">Media Toolkit</h1>

<p align="center">
  Download videos, record live streams, and turn any video into text you can paste into an AI chat.<br>
  Everything runs on your own Windows PC.
</p>

<p align="center">
  <a href="https://github.com/AnotherAH/media-toolkit/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/AnotherAH/media-toolkit?style=flat-square&color=2c6ae0"></a>
  <img alt="Windows 10 and 11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-2c6ae0?style=flat-square">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-2c6ae0?style=flat-square"></a>
  <a href="https://github.com/AnotherAH/media-toolkit/actions/workflows/ci.yml"><img alt="Tests" src="https://img.shields.io/github/actions/workflow/status/AnotherAH/media-toolkit/ci.yml?style=flat-square&label=tests"></a>
</p>

<p align="center">
  <img src="docs/images/download.png" alt="The Download tab with a video preview and format choices" width="900">
</p>

## What it does

**Download from 1,700+ sites.** YouTube, Instagram, TikTok, X, Reddit, Vimeo,
Facebook, Twitch and many more. Pick a quality, save only the audio, cut out
a part, grab a whole playlist or just the newest videos of a channel, add
subtitles, skip sponsor segments, or convert the result with your graphics
card.

**Transcripts in seconds.** Paste a link and get clean text, ready for any AI
chat. If the video has captions you have them in about a second; if not, the
speech is transcribed on your PC with [Whisper](https://github.com/SYSTRAN/faster-whisper),
using your NVIDIA graphics card when you have one. Drop a local video or
audio file to transcribe it too. Save as text, notes with timestamps,
subtitles or JSON.

**Record live streams.** Video and sound together, stop whenever you like and
keep everything recorded so far. It can wait for a scheduled stream to start,
split long recordings into parts, and survives short network drops.

<table>
  <tr>
    <td width="50%"><img src="docs/images/transcript.png" alt="A transcript with Copy for AI chat"></td>
    <td width="50%"><img src="docs/images/queue.png" alt="The queue with finished and running jobs"></td>
  </tr>
  <tr>
    <td align="center"><sub>Transcripts ready to paste into an AI chat</sub></td>
    <td align="center"><sub>Every job with progress, errors and a way forward</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/images/live.png" alt="Recording a live stream"></td>
    <td width="50%"><img src="docs/images/settings-light.png" alt="Settings in the light theme"></td>
  </tr>
  <tr>
    <td align="center"><sub>Live recording with stop and save</sub></td>
    <td align="center"><sub>Light and dark themes follow Windows</sub></td>
  </tr>
</table>

## Install

1. Download **`MediaToolkit-Setup-1.2.0.exe`** from the
   [latest release](https://github.com/AnotherAH/media-toolkit/releases/latest).
2. Run it. It installs for your user only, so there is no administrator
   prompt, and adds Start menu and desktop shortcuts.
3. The installer is not code-signed yet, so Windows SmartScreen may say it
   "protected your PC". Choose **More info › Run anyway**. You can compare the
   file with `SHA256SUMS.txt` from the same release first:
   `certutil -hashfile MediaToolkit-Setup-1.2.0.exe SHA256`

Prefer not to install? Download the **portable zip**, unpack it anywhere
(a USB stick works) and run `MediaToolkit.exe`. It keeps its settings and
models in a `data` folder next to it.

Everything the app needs is included: Python, yt-dlp and ffmpeg. Two things
are downloaded later, only if you want them:

| Download | Size | When |
|---|---|---|
| Speech model | 75 MB to 3 GB | The first time a video without captions is transcribed |
| GPU support (NVIDIA cuBLAS) | 528 MB | Offered in Settings on PCs with an NVIDIA graphics card |

Upgrading keeps your settings, models and files. The uninstaller asks before
removing the app's data folder, and never deletes your downloads.

## First steps

1. **Get a transcript:** open the Transcript tab, paste a video link and press
   Enter. Press **Copy for AI chat** and paste it into any chatbot.
2. **Download a video:** paste a link on the Download tab, check the preview,
   choose Video or Audio only, press Enter.
3. **Private or age-restricted videos:** go to Settings › Sign-in for private
   videos and let the app use the sign-in from your browser.

When a site stops working, it has usually changed something that yt-dlp has
already fixed: **Settings › About › Site support** checks for an update.

## Privacy

There is no account, no tracking and no telemetry. The app talks only to:

- the sites you paste links from (through yt-dlp), and SponsorBlock when you
  turn on sponsor skipping;
- Hugging Face, to download a speech model the first time one is needed;
- PyPI, to download GPU support or a yt-dlp update when you ask for it;
- GitHub, when you press Check for updates.

Your files, transcripts and sign-in cookies stay on your PC. The app's
window talks to a small server on `127.0.0.1` that refuses requests from
websites, other computers and anything without the per-launch key.

## Command line

```text
MediaToolkit.exe                       open the app (or return to the running one)
MediaToolkit.exe --port 9000           use a fixed port
MediaToolkit.exe --browser             open in your normal browser instead of the app window
MediaToolkit.exe --server              run without a window; keeps going until stopped
MediaToolkit.exe --diagnose MODEL      write a report on a speech model to diagnose.txt
MediaToolkit.exe --diagnose MODEL --repair   ...and download that model again
MediaToolkit.exe --version             print the version
```

`--host` can make the server reachable from other devices on your network; it
then prints an address with an access key and requires it on every request.
Only use it on a network you trust.

The app keeps its log in `%LOCALAPPDATA%\Media Toolkit\app.log`
(Settings › About › Open log file).

## Building from source

Needs Windows 10/11 and Python 3.11 or newer (3.13 recommended).

```bat
git clone https://github.com/AnotherAH/media-toolkit.git
cd media-toolkit
setup.bat
Start.bat
```

`setup.bat` creates a private environment in `.venv`, installs the
dependencies and downloads ffmpeg into `bin\`. To build the installer and the
portable zip you also need [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install JRSoftware.InnoSetup`):

```bat
.venv\Scripts\python.exe -m pip install -r requirements-build.txt -c requirements-lock.txt
.venv\Scripts\python.exe tools\build.py
```

The results land in `dist\`. Pushing a `v<version>` tag builds the same files
on GitHub Actions into a draft release. See [CONTRIBUTING.md](CONTRIBUTING.md)
for tests and guidelines.

## Responsible use

Media Toolkit is a tool for saving and transcribing media you have the right
to use: your own uploads, public-domain and Creative Commons works, content
whose license allows it, or personal copies where your local law permits
them. Respect copyright and the terms of the sites you use. Do not use it to
redistribute other people's work.

Media Toolkit is not affiliated with or endorsed by YouTube, Google,
Instagram, Meta, TikTok, X, Twitch or any other site or service it
mentions. Their names are used only to describe what the app
works with.

## Built on

Media Toolkit is a thin layer over excellent open-source projects:

| Project | What it does here |
|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Every site, format selection, playlists, subtitles, SponsorBlock, live stream links |
| [FFmpeg](https://ffmpeg.org) ([yt-dlp builds](https://github.com/yt-dlp/FFmpeg-Builds)) | Joining, converting, recording and audio decoding |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and [CTranslate2](https://github.com/OpenNMT/CTranslate2) | Speech recognition |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Skipping silence |
| [SponsorBlock](https://sponsor.ajay.app) | Sponsor segment data (CC BY-NC-SA 4.0) |
| [FastAPI](https://github.com/fastapi/fastapi) and [Uvicorn](https://github.com/encode/uvicorn) | The local server behind the window |

The installed app includes the license of every component in
`THIRD-PARTY-NOTICES.txt` (also under Settings › About › Third-party
licenses). Screenshots show Blender Foundation open movies
([CC BY 3.0](https://creativecommons.org/licenses/by/3.0/), © Blender
Foundation | blender.org).

## License

[MIT](LICENSE). The installer also contains third-party software under its
own licenses, including FFmpeg under the GPL; see `THIRD-PARTY-NOTICES.txt`.
