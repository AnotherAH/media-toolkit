# Security policy

## Supported versions

Only the latest release gets security fixes. Update from the
[Releases page](https://github.com/AnotherAH/media-toolkit/releases/latest).

## Reporting a vulnerability

Please report security problems privately through
[GitHub's private vulnerability reporting](https://github.com/AnotherAH/media-toolkit/security/advisories/new)
rather than in a public issue. Include what you found, how to reproduce it, and
which version you tested. You should get a reply within a week.

## How the app is built to be safe

Media Toolkit runs a small web server that only listens on `127.0.0.1`, and
shows its interface in an app window. Because any website you visit could try
to talk to a local server, the server:

- accepts requests only when the `Host` header names the local address it
  is listening on, which blocks DNS-rebinding attacks;
- rejects requests whose `Origin` is another website;
- requires a random per-launch token on every request that changes something;
- opens and reveals files only inside your download and transcript folders.

Downloads the app makes on your behalf (ffmpeg repair, GPU support, yt-dlp
updates) come from their official sources over HTTPS and are checked against
published SHA-256 hashes before use.

Sign-in cookies you give the app are stored only on your PC, in the app's data
folder, and are used only for the sites they belong to.
