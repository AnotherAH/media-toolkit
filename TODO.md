# To do

Work that is known and deliberately left for a later release.

## Testing

- [ ] A full visual pass over every screen in both themes and at a narrow
      window size, checking spacing, wording and states against each other.
- [ ] A hands-on run of every flow in the real app window: first-run setup
      both ways, several links at once, a playlist with a quick update, a
      dropped local file, stopping a live recording, settings surviving a
      restart, switching between graphics card and processor.
- [ ] Close the window while a download runs, then start the app again and
      check it reattaches to the running copy (tested only headless so far).
- [ ] SponsorBlock cutting on a video that really has sponsor segments (tested
      only on videos without any).
- [ ] Waiting for a scheduled premiere on the Download tab against a real
      premiere (covered by unit tests only).
- [ ] Install the 1.2.0 installer over an existing 1.1.1 install and check the
      upgrade keeps settings and models.

## Features and polish

- [ ] When the window is closed while work is running, ask "Keep downloading"
      or "Stop and quit" instead of the one-line notice.
- [ ] Two-pass loudness normalisation (single pass lands within about 2 LU of
      -14 LUFS on short clips).
- [ ] Remove a cancelled download's temporary folder as soon as the job is
      removed, instead of in the next day's clean-up.
- [ ] Live recordings: when a stream's playlist stops advancing without ending,
      each 90-second stall starts a new part that repeats the last few seconds.
- [ ] Pick the right graphics card on machines with more than one NVIDIA GPU.

## Release and legal

- [ ] Sign the installer so Windows SmartScreen stops warning on first run.
- [ ] List every Rust crate's copyright line in THIRD-PARTY-NOTICES.txt for the
      packages that bundle Rust code (pydantic-core, tokenizers, hf-xet); today
      the notices point to each project's Cargo.lock.
- [ ] When the repository goes public: turn on private vulnerability reporting
      (SECURITY.md links to it) and upload a social preview image.
