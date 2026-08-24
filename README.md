# Media Toolkit

A standalone Windows app that does three things:

1. **Downloads video and audio** from YouTube, Instagram, TikTok, X, Reddit, Vimeo, Facebook and ~1,750 other sites.
2. **Records live streams** with video and audio together, stoppable at any moment.
3. **Turns any video into a transcript** you can paste straight into ChatGPT, Claude, or any other chatbot.

Everything runs on your own machine. Nothing is uploaded anywhere.

---

## Install

Run **`MediaToolkit-Setup-1.1.1.exe`** (150 MB). It installs per-user, so there is no
administrator prompt and no UAC dialog, and it adds Start Menu and desktop shortcuts.
Running it over an existing install **upgrades in place** -- one entry in Add/Remove
Programs, and your settings, downloads, transcripts, models and GPU pack are untouched.

Media Toolkit then runs as a **standalone desktop app** -- its own window, no browser tabs,
no address bar, no console window. Closing the window quits it. Nothing else needs to be
installed: Python, yt-dlp, ffmpeg and the speech engine are all inside the installer.

Two things are fetched later, only if they are useful to you:

| Pack | Size | When |
|---|---|---|
| GPU acceleration (CUDA cuBLAS) | 740 MB | Offered in Settings if you have an NVIDIA GPU. Without it Whisper still runs, on the CPU. |
| — | | Everything else, ffmpeg included, ships in the installer. |

Files live in two places, deliberately: the program in
`%LOCALAPPDATA%\Programs\Media Toolkit`, and your settings, downloads, transcripts and
Whisper models in `%LOCALAPPDATA%\Media Toolkit`. Upgrading never touches your data, and
the uninstaller asks before deleting it.

On first launch a short wizard asks where to save files, what quality you want by default,
which Whisper model to use (it recommends one based on your GPU), and optionally helps you
sign in to sites. Re-run it any time from **Settings -> Run first-time setup again**.

### Running from source instead

```
setup.bat        once, to create the environment
Start.bat        to launch
```

Requires Python 3.10+. Installing [Node.js](https://nodejs.org) is also worth it: yt-dlp
needs a JavaScript runtime for full YouTube support. Settings tells you whether it found one.

### Building the installer yourself

```
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean MediaToolkit.spec
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\MediaToolkit.iss
```

The spec deliberately excludes the NVIDIA CUDA wheels. Bundling them would add 2 GB, and
measurement showed only cuBLAS is needed -- CTranslate2 ships its own cuDNN shim and routes
Whisper through cuBLAS, so the 1.25 GB of cuDNN buys nothing.

---

## Downloading

Paste one link or many (one per line) and hit Download.

**Format**

| Option | What it does |
|---|---|
| Video + audio / Audio only | MP4/MKV/WebM, or MP3/M4A/Opus/FLAC/WAV |
| Quality | Best, 4K, 1440p, 1080p, 720p, 480p, smallest, or "Best MP4 (H.264)" using yt-dlp's own compatibility sort order |
| Show every available format | A full table of every stream — codec, resolution, fps, bitrate, size |
| Embed cover art / metadata / chapters | Title, artist, date and thumbnail written into the file |
| SponsorBlock | Cuts sponsor and self-promo segments out of YouTube videos, or just marks them as chapters |

**Subtitles, playlists and clipping**

Official or auto-generated subtitles in any language, optionally embedded. Whole playlists
and channels with a skip-list so re-runs only fetch what is new, reverse or shuffled order,
item ranges, a stop-after-N limit, and "stop as soon as a known item appears" for
incremental channel syncing. Clip by time (`1:30-4:15`, `*10:00-inf`) or by chapter name
(`intro,outro`). Split a long video into one file per chapter. Remove chapters matching
a pattern. Join a whole playlist into a single file.

**Filters** — only download items that match

Shortest/longest duration, minimum views, title text, upload date range, maximum file size,
and skip-live-streams. Point it at a channel and get only the things you actually want.

**Re-encode, live streams and extra files**

| Option | What it does |
|---|---|
| Re-encode video | Hardware encoding via NVENC, Quick Sync or AMF — whatever your machine has. AV1, H.265 or H.264. |
| Re-encode quality | High / Balanced / Small file |
| Normalise loudness | EBU R128 to −14 LUFS, the streaming standard |
| Convert thumbnails | WebP → JPG or PNG |
| Live streams | Record from the start, or wait for a scheduled premiere (see the **Live** tab for full recording) |
| Extra files | Metadata JSON, description, **top N comments**, thumbnail image, shortcut back to the page |

Comments are capped (default 200, sorted by top) — uncapped extraction never finishes on
a popular video.

**Network** (Settings)

Speed limit, parallel connections, proxy, forced IPv4, region spoofing, request delays for
rate-limit-prone sites, optional aria2c, and **browser TLS impersonation** — 38 Chrome/Edge/
Safari fingerprints via curl_cffi, which gets past sites that block non-browser traffic.

---

## Recording live streams

Paste a stream link on the **Live** tab. It works anywhere the app works -- YouTube, Twitch,
Kick, TikTok, Instagram, Facebook, news channels, radio -- because yt-dlp resolves the
stream and ffmpeg does the recording.

**Video and audio are recorded together into one file.** Many sites serve the picture and
the sound as two separate streams; both are pulled at once and muxed as they arrive. Nothing
is re-encoded, so there is no quality loss and almost no CPU cost.

| Option | What it does |
|---|---|
| Quality | Best, 4K, 1440p, 1080p, 720p, 480p |
| Save as | MP4, MKV, or the raw `.ts` |
| Stop after N minutes | Ends the recording on its own |
| Split every N minutes | One file per chunk, useful for long sessions |
| Wait for the stream to start | Polls a scheduled or offline stream, with backoff, and starts the moment it goes live |
| Audio only | For radio and talk streams |

**Stop is a real feature, not an abort.** Press *Stop recording* in the Queue and the file is
finalised properly with everything captured so far, then wrapped to MP4. The recording is
written as MPEG-TS while it runs, which has no index to write at the end -- so a stop, a
crash, or a dropped connection still leaves a complete, playable file. ffmpeg is also told to
reconnect automatically, so an overnight recording survives a network blip.

While recording you get elapsed time, file size and bitrate, and the tab shows a live counter.

---

## Transcripts

Paste a link and hit Transcribe, or drop a local video/audio file onto the page.

The app tries the cheap path first: **if the site already has captions, you get the
transcript in about a second.** An 18-minute YouTube video came back in 2.4 seconds in testing.

If there are no captions — or you tick *Always use Whisper* — the audio is downloaded and
transcribed locally with Whisper. On an RTX 5080 that runs at roughly **27x realtime**.

Results come in five shapes, switchable with one click:

- **Clean text** — paragraphs, no timestamps. This is the chatbot format.
- **Timestamped** — Markdown with clickable YouTube deep-links every 30 seconds.
- **SRT** / **VTT** — subtitle files for video players.
- **JSON** — segments with start/end times, for scripting.

Buttons that matter:

- **Copy for AI chat** — copies the transcript with a title/channel/source header attached.
- **Split into chunks** — breaks a long transcript into ~12,000-character parts so each
  one fits in a single chatbot message, with next/previous navigation.

Word count and estimated token count are shown so you know what you are pasting.

---

## Whisper models

Models download on first use, into `%LOCALAPPDATA%\Media Toolkit\models` as plain files.
Settings lists what you have, whether each one is intact, and offers a re-download.

Three things this app does that the underlying library does not:

- **Progress.** faster-whisper downloads with the progress bar hardcoded off. A 3 GB
  download with no feedback looks like a hang, which is how people end up closing the app
  mid-download and corrupting the cache. Progress is measured from bytes landing on disk,
  so it works regardless of which transport Hugging Face picks.
- **Verification and repair.** The weights are opened, not just stat-ed, before use. A
  damaged or half-downloaded model is detected and re-fetched automatically instead of
  failing forever with "Unable to open file 'model.bin'".
- **Real files, not symlinks.** Hugging Face's default cache keeps one copy under `blobs/`
  and symlinks to it from `snapshots/`. On Windows those links are not reliably followable
  — measured here with the blob present and complete at 145 MB, Windows still answered
  "cannot find the path specified" when opening the link, and `os.path.realpath` masked it
  by falling back to string joining. Models are downloaded as ordinary files instead.

Models downloaded by an earlier version are reused where they still work, so upgrading
never re-downloads gigabytes unnecessarily.

If a model ever misbehaves, `MediaToolkit.exe --diagnose <model>` prints exactly what the
packaged build sees — paths, sizes, and whether each file opens.

---

## Hardware

The transcription backend is detected, then **proven** — every candidate is loaded and
run on real audio before the app commits to it. If a backend fails, the next one is tried
automatically, so the app never dead-ends on a driver problem.

| Your GPU | What it uses |
|---|---|
| RTX 50-series (Blackwell) | `float16` first — int8 kernels have been unreliable on sm_120 |
| RTX 20/30/40-series | `int8_float16` first — fastest and smallest on those INT8 tensor cores |
| GTX 10-series (Pascal) | `int8`, then `float32` — no usable fp16 throughput on that generation |
| Non-NVIDIA, or no GPU | CPU `int8`, multi-threaded |

Model choices that will not fit in your VRAM are greyed out in Settings, and the app
suggests one that will. You can always override device and precision by hand.

**Even with no GPU at all, the caption path still returns transcripts instantly** — Whisper
is only needed when a video has no captions of its own.

---

## Signing in to sites

Needed only for private, members-only or age-restricted content — Instagram in particular
refuses to serve most posts anonymously.

There is no such thing as a fake session cookie: sites validate the session server-side, so
a fabricated value authenticates nothing. What the app removes is all the manual work.
Three routes, in order of least effort:

1. **Detect my browsers** — tries every browser installed on the machine, reports which ones
   actually hand over cookies and which sites you are signed in to, and picks the best one.
2. **Sign in here** — opens a browser window the app controls, you log in normally, and it
   captures the cookies for you over the DevTools protocol. This is the reliable route on
   Windows, where Chrome and Edge seal their cookie stores with app-bound encryption that
   no external tool can read.
3. **Paste cookies** — accepts a whole `cookies.txt`, or just a
   `name=value; name=value` header copied from dev tools, and writes a proper cookie file.

---

## Keeping it working

Sites change their internals constantly and extractors break. **Settings → Update yt-dlp**
pulls the newest version; restart the app afterwards. That fixes the large majority of
"this used to work" failures.

---

## What is under the hood

| Project | Role |
|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Every site extractor, format selection, playlists, filters, SponsorBlock, chapters, cookies, impersonation, live stream resolution |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + [CTranslate2](https://github.com/OpenNMT/CTranslate2) | Local speech recognition |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Skips silence so long videos transcribe faster |
| [FFmpeg](https://github.com/yt-dlp/FFmpeg-Builds) | Muxing, hardware encoding, loudness normalisation, metadata, cover art |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | Browser TLS fingerprint impersonation |
| [yt-dlp-ejs](https://github.com/yt-dlp/ejs) + Node | JavaScript challenges for full YouTube support |
| [FastAPI](https://github.com/fastapi/fastapi) | The local server |

The app itself is a thin layer over these — option mapping, caption parsing and cleanup,
backend probing, job orchestration, and the UI. The one piece of real custom logic is
`app/recode.py`: yt-dlp's video converter deliberately skips a file that is already in the
target container, which is exactly the case where you wanted the re-encode, so the app
ships a small postprocessor that keys off the codec instead. `app/live.py` is the other:
yt-dlp can download a live stream but hands it to ffmpeg internally and reports nothing back,
so there is no progress and no way to stop and keep the file -- which for live is the entire
feature. The app drives ffmpeg itself and lets yt-dlp do what it is good at, resolving the
stream.

---

## Layout

```
app/          config, hardware detection, yt-dlp engine, whisper engine, caption
              parsing, cookie discovery, ffmpeg presets, app window, FastAPI routes, UI
bin/          bundled ffmpeg
installer/    Inno Setup script
tools/        icon generator, ffmpeg fetcher
MediaToolkit.spec   PyInstaller build definition
```

Installed layout:

```
%LOCALAPPDATA%\Programs\Media Toolkit\   program files (removed on uninstall)
%LOCALAPPDATA%\Media Toolkit    config.json      settings          models\     Whisper models
    downloads\       finished media    runtime\    on-demand GPU pack
    transcripts\     finished text     app.log     startup diagnostics
```

---

## Command line

```
MediaToolkit.exe --port 9000            pick a port
MediaToolkit.exe --browser              open in your normal browser instead
MediaToolkit.exe --no-browser           server only, no window
MediaToolkit.exe --server               headless; keeps running with no window
```

The HTTP API is documented at `/api/docs` while the app is running.
