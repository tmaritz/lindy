# lindy

Takes the usual "trending repos" sources and re-sorts them by how likely a
project is to still be maintained in a few years, rather than by how much
noise it is making this week.

Named after the Lindy effect: something that has already lasted a long time
and is still active will probably keep going.

## Sources

- Hacker News: stories over 120 points that link to a GitHub repo (Algolia API)
- GitHub Trending: daily and weekly, scraped from the page HTML
- Trendshift: not wired up yet. The site renders client-side and I haven't
  found a usable API.

## Usage

    python3 lindy.py                     # writes LINDY.md and index.html
    python3 lindy.py --limit 80 --min-score 20
    python3 lindy.py --json lindy.json   # also dump the raw records

No dependencies, standard library only. It picks up a GitHub token from
`GITHUB_TOKEN` or `gh auth token` if one is set, which raises the API limit
from 60 to 5000 requests an hour.

## Score

0 to 100, added up and then clamped:

- age, up to 25, maxed at two years
- how many of the last 12 months had commits, up to 30
- contributor count, up to 20, maxed at 10 people
- has tagged releases, 8
- minus up to 45 if it is under three months old and already past 3k stars
- minus up to 20 the longer it has been since the last push, past three weeks

The weights are guesses. Adjust them in `score()` against repos you already
have an opinion about.
