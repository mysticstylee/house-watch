# House Watch 🏠

[![House Watch](https://github.com/mysticstylee/house-watch/actions/workflows/house-watch.yml/badge.svg)](https://github.com/mysticstylee/house-watch/actions/workflows/house-watch.yml)

A small bot that watches one specific house and pings a Discord channel the moment it goes up for sale. It runs for free on GitHub Actions, so no computer has to stay on.

## What it checks

| Source | How |
| --- | --- |
| **Zillow** | Reads the house's own page and its status. Also covers for-sale-by-owner posts. |
| **Realtor.com** | Searches every for-sale and pending listing in the house's ZIP code for the address. |
| **Local MLS** | Scans every active, pending and under-contract listing in the ZIP, across all agencies. |

Any one of the three showing the house as listed sends the alert.

## When it checks

- Every 15 minutes, from 5am to midnight Central.
- Once an hour, from midnight to 5am.

GitHub sometimes starts scheduled runs a few minutes late when its servers are busy.

## What it sends

- **Listed:** an `@everyone` ping with the status, price and a link, the moment any source shows the house for sale, coming soon or pending.
- **After that:** price changes, the sale going pending, sold, or the listing being taken down.
- **Health:** a message if one source fails 8 checks in a row, and another when it recovers.
- **First run:** a single "House Watch is live" message so you know it's working.

## Setup

The address and the Discord webhook are never stored in this repo. Both are repository secrets.

1. Go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add `HOUSE_ADDRESS`, written like `742 Evergreen Ter, Springfield, MO 65801`.
3. Add `DISCORD_WEBHOOK_URL`. In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
4. Open the **Actions** tab, pick **House Watch**, and press **Run workflow**. A "House Watch is live" message should land in the channel.

Until both secrets exist, runs finish with a "not set up yet" warning and check nothing.

## Privacy

- The address only ever exists as a secret. It is stripped from the run logs, which are public on a public repo.
- What the bot saw on its last check is kept in the Actions cache instead of being committed.

## Run it yourself

```bash
pip install -r requirements.txt
export HOUSE_ADDRESS="742 Evergreen Ter, Springfield, MO 65801"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

python house_watch.py --test    # send a test message
python house_watch.py --once    # run one check
python house_watch.py           # keep running on the schedule
```

## Notes

- The local MLS search is set up for the Ste. Genevieve, Missouri area. For another area, point `MLS_SEARCH_URL` at a different IDX search.
- It can't see a sale that never goes online, such as a yard sign or a private deal.
