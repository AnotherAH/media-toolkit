# Changelog

All notable changes to Media Toolkit are listed here. Versions follow
[semantic versioning](https://semver.org/).

## [1.2.0] - 2026-09-24

A large update: a redesigned interface, a security overhaul, and fixes for
more than a hundred problems found in a full review of the app.

### New

- **A redesigned interface.** Each tab shows the work it started, with
  progress, the result and any error in place, so you are never sent to
  another tab. A calm light and dark theme that follows Windows, keyboard
  shortcuts (Enter to start, Ctrl+1 to Ctrl+5 for tabs), and full
  right-to-left support for Persian, Arabic and Hebrew titles and transcripts.
- **Readable errors with a fix button.** "This video is private" with
  [Set up sign-in], "ffmpeg is missing" with [Repair], "The site is limiting
  downloads" with [Try again], and the technical details one click away.
- **Choices are remembered.** Quality, format and extras on the Download tab,
  transcription and recording options are kept between launches.
- **Playlist controls where they belong.** Paste a playlist or channel and
  choose All, the first or latest N, or specific items, with filters, right
  in the link preview.
- **Transcripts in the right language.** One Language menu, including
  "Translate to English", replaces four settings. The reader shows clean
  paragraphs or timestamps, and Copy for AI chat adds the title and link.
  Long transcripts can be copied in parts.
- **Files open instead of downloading again.** Play, Open and Show in folder
  on every finished job.
- **The queue survives a restart**, with Try again, Remove, and Undo for
  Clear completed.
- **Portable version.** A zip that keeps its settings, models and downloads in
  its own folder.
- **Site support updates work in the installed app** (Settings › About):
  newer yt-dlp versions are downloaded, verified and used after a restart.
- **About section** with the version, update check, licenses, and a button to
  copy system details for bug reports.
- **Single instance.** Opening the app again returns to the running one, and
  downloads keep going in the background if you close the window.

### Fixed

- Live recordings split into parts were cut off after about 25 seconds.
- A short network drop ended a live recording; it now reconnects and keeps
  recording.
- Waiting for a scheduled live stream gave up after about 6 minutes.
- Stopping a recording showed it as cancelled while the file was still being
  saved.
- "Stop after N items" and "Stop at the first video I already have" reported
  a failure and lost the files that had been downloaded.
- A clip of a video was saved under the full video's name and could replace
  it; downloading the same video again at another quality returned the old
  file.
- Vertical videos (Shorts, Reels) were downloaded at a lower resolution than
  asked for.
- "Split by chapters" wrote the files into the wrong folder; "Join into one
  file" did nothing; "Normalise loudness" only worked with a re-encode.
- WebM and WAV downloads failed when cover art was on.
- Cancelling a playlist kept downloading the remaining items.
- Hardware video encoders were offered on machines without that hardware.
- "Translate to English" and the language choice were ignored when a video
  had captions, and non-English videos could get a machine-translated
  transcript without saying so.
- "Split into chunks" did nothing for transcripts without punctuation or in
  Chinese, Japanese or Korean.
- Folder names with non-English letters were garbled by the folder picker.
- Jobs running at the same time could corrupt the saved sign-in cookies.
- The Proxy setting was ignored by live recording and model downloads.
- A damaged settings file silently reset every setting.
- Three live recordings used up every download slot.
- `--diagnose` printed nothing in the installed app and deleted the model it
  was meant to check.

### Security

- The local server now accepts requests only from its own window: it checks
  the Host and Origin of every request and requires a per-launch key, so
  websites you visit cannot control the app.
- Files can only be opened or revealed inside your download and transcript
  folders, and uploaded file names can no longer point outside the app's
  temporary folder.
- The sign-in browser no longer leaves a debugging port open, and a pasted
  cookie header is saved only for the site it belongs to.
- ffmpeg repairs, GPU support and yt-dlp updates are checked against
  published SHA-256 hashes before use.

### Licensing and privacy

- Media Toolkit is now released under the MIT license.
- The installer ships the license texts of every component it contains
  (`THIRD-PARTY-NOTICES.txt`).
- GPL-licensed libraries no longer run inside the app: audio is decoded by the
  bundled ffmpeg program instead, and NVIDIA's cuDNN is no longer included.
- ffmpeg is now a pinned release build (7.1.1) whose source is permanently
  available.
- Telemetry from bundled libraries (Hugging Face Hub, ONNX Runtime) is turned
  off.

### Changed

- The installer is smaller (127 MB, was 150 MB) and installs for the current
  user only, with no administrator prompt.
- New downloads default to Videos › Media Toolkit and transcripts to
  Documents › Transcripts; the uninstaller never deletes them.
- Files are dated when they are downloaded unless you choose the upload date.

## [1.1.1] - 2026-08-24

First release.

- Download video and audio from YouTube and about 1,700 other sites, with
  quality presets, playlists, subtitles, clipping, SponsorBlock, chapter
  tools and hardware re-encoding.
- Record live streams with video and sound together, stoppable at any time.
- Turn videos into transcripts from the site's captions, or with Whisper on
  your own PC, in five formats.
- Windows installer with an optional GPU pack for fast transcription.

[1.2.0]: https://github.com/AnotherAH/media-toolkit/releases/tag/v1.2.0
[1.1.1]: https://github.com/AnotherAH/media-toolkit/releases/tag/v1.1.1
