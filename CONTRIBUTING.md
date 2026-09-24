# Contributing

Thanks for wanting to help. Bug reports, fixes and small focused features are
all welcome.

## Before you start

- **Site stopped working?** Most "this used to work" problems are a site
  change that yt-dlp has already fixed. Check for an update under **Settings › About ›
  Site support** first. If it still fails, report it to
  [yt-dlp](https://github.com/yt-dlp/yt-dlp/issues) unless the problem is in
  Media Toolkit itself.
- For anything larger than a bug fix, open an issue first so we can agree on
  the approach.

## Running from source

Requires Windows 10/11 and Python 3.11 or newer.

```bat
git clone https://github.com/AnotherAH/media-toolkit.git
cd media-toolkit
setup.bat
Start.bat
```

`setup.bat` creates a private virtual environment in `.venv`, installs the
dependencies and fetches ffmpeg into `bin\`.

Set `MEDIA_TOOLKIT_HOME` to a scratch folder to keep test settings, models and
downloads away from your real ones:

```bat
set MEDIA_TOOLKIT_HOME=%TEMP%\mt-dev
Start.bat
```

## Tests

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

Tests that need the network are skipped by default; run them with
`pytest --network`.

## Building the installer

```bat
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe tools\build.py
```

This needs [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install JRSoftware.InnoSetup`). The installer, the portable zip and
`SHA256SUMS.txt` land in `dist\`.

## Guidelines

- Keep it small. The app is a thin layer over yt-dlp, ffmpeg and
  faster-whisper; prefer using what they already do over reimplementing it.
- The interface is plain HTML, CSS and JavaScript modules with no build step
  and no external requests. Please keep it that way.
- Every user-facing string should be plain language, without tool names or
  error codes.
- Titles and transcripts can be in any script; render user-supplied text with
  `dir="auto"`.
- Do not add examples or tests that download copyrighted material. Use
  public-domain or Creative Commons media such as Blender's open films.

By contributing you agree that your contribution is licensed under the
project's [MIT license](LICENSE).
