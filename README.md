# Pitchside

A phone-friendly football app: world news with photos, fixtures with must-see games, highlights, podcasts, and a team you follow.

- `index.html` is the whole app (no build step).
- `data.json` and `photos/` hold the latest news, fixtures and badges. A GitHub Action (`.github/workflows/daily-update.yml`) refreshes them every morning from ESPN's free public football data.
- `badges/` holds club badges used as a fallback.

Hosting: import this repository on Vercel as a static site (no settings needed). Every push, including the daily update, redeploys it.
