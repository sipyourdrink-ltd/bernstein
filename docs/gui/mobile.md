---
title: Mobile PWA + tunnel onboarding
description: Install the Bernstein dashboard as a phone home-screen PWA via a Cloudflare / ngrok / bore / Tailscale tunnel with QR onboarding.
tags:
  - gui
  - pwa
  - mobile
  - tunnel
---

# Mobile PWA + tunnel onboarding

The Bernstein web GUI ships as an installable PWA. Pair it with the built-in tunnel wrapper and you get a one-key phone handoff: scan a QR, tap "Add to Home Screen", you are in.

## TL;DR

```bash
bernstein gui serve --tunnel
```

Prints a Cloudflare (or ngrok / bore / Tailscale) URL, a 6-word passphrase, and an ASCII QR. Scan it on your phone, install to home screen, type the passphrase once.

## Surfaces

| Asset                            | Path                              | Notes                                   |
|----------------------------------|-----------------------------------|-----------------------------------------|
| Manifest                         | `/manifest.webmanifest` + `/ui/`  | `application/manifest+json` media type  |
| Service worker                   | `/sw.js` + `/ui/sw.js`            | `Service-Worker-Allowed: /` header      |
| Offline fallback                 | `/ui/offline.html`                | Self-contained, no external assets      |
| App icons                        | `/ui/icon-192.png`, `/ui/icon-512.png` | Programmatically rendered, maskable |
| SPA shell                        | `/ui/`                            | Existing Vite+React mount               |

## CLI

```text
bernstein gui serve --tunnel [--tunnel-provider <auto|cloudflared|ngrok|bore|tailscale>]
bernstein gui qr [--url URL] [--rotate] [--passphrase-file PATH]
```

`--tunnel` boots the GUI, opens a tunnel through the selected driver (auto-detected by default), issues a fresh URL-safe bearer token and a 6-word diceware passphrase, persists both to `~/.bernstein/dashboard.passphrase` (0600), and prints a QR.

`bernstein gui qr` reprints the last QR. Pass `--rotate` to issue fresh credentials in place without restarting the tunnel.

## Offline behaviour

The service worker pre-caches the SPA shell (`index.html`, manifest, offline page, icons) on install. Navigations are network-first; if the network fails, it serves cached `/ui/index.html`, falling back to `/ui/offline.html` if the shell is unavailable.

Runtime stale-while-revalidate caching applies only to these GET paths (and their subpaths):

- Legacy: `/api/projects` and `/api/cost`
- v1: `/api/v1/projects` and `/api/v1/cost`

A response is added to the runtime cache only after a successful HTTP 200, so an offline read can use cached data only if that same endpoint was successfully read while online before. The v1 Fleet screen instead calls `apiGet('/fleet/projects')`, which resolves to `/api/v1/fleet/projects`; that path is not in the service worker's runtime list. The app shell can therefore load offline, but the service worker does not provide a cached Fleet projects snapshot (a fresh offline Fleet load reports a project-read error).

## Onboarding URL anatomy

```
https://<tunnel>.trycloudflare.com/ui/#t=<urlsafe-token>
```

The token lives in the URL fragment so it never appears in webserver access logs. The SPA reads it once on first load, stashes it in `localStorage`, and scrubs the fragment from the URL.

## Acceptance checklist

| Behaviour                                                     | How to test                                       |
|---------------------------------------------------------------|---------------------------------------------------|
| QR scans and opens the SPA at the tunnel URL                  | `bernstein gui serve --tunnel`, scan with phone   |
| iOS "Add to Home Screen" promotes the app                     | Safari -> share sheet -> Add to Home Screen       |
| Android "Install app" prompt fires                            | Chrome -> menu -> Install app                     |
| Offline Fleet load uses the cached shell, not a service-worker Fleet snapshot | Open Fleet online, then load it in a fresh page while offline; expect the Fleet project read to fail |
| Rotating credentials does not break the existing tunnel       | `bernstein gui qr --rotate`                       |
