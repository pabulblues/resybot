# resybot

A personal reservation sniper for Resy. It waits for a restaurant's release
moment, polls availability, picks the best slot inside your time window, and
books it, usually within a second of the drop.

**Read this first.** Resy's terms prohibit automated access. Accounts have
been banned for it. This is for your own dining, not for resale. Run it
gently (the defaults are tuned for that), and consider a secondary Resy
account for testing.

---

## 1. Setup (10 minutes)

```bash
cd resybot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
cp targets.example.yaml targets.yaml
```

### Getting your token (the only secret you need)

1. Open https://resy.com in Chrome and log in.
2. Press **F12** (or Cmd+Opt+I) → **Network** tab. Tick "Preserve log".
3. Click any restaurant and pick a date so the page loads availability.
4. In the Network list, click a request whose name starts with **`find`**
   (URL contains `api.resy.com/4/find`).
5. In the **Headers** pane, scroll to **Request Headers**. Copy:
   - `x-resy-auth-token:` → the long string → `RESY_AUTH_TOKEN` in `.env`
   - `authorization: ResyAPI api_key="XXXX"` → just the XXXX part → `RESY_API_KEY`
6. Never share these, never commit `.env`.

The token lasts weeks to months. If `check` ever says *401 Unauthorized*,
repeat these steps.

### Notifications

Nothing to set up. On your Mac you get a notification with a sound when
something books (or doesn't). For captcha restaurants the Resy page opens
in your browser at the drop, on the right date, so you just click the time
and confirm.

---

## 2. Fill in targets.yaml

```yaml
party_size: 2
timezone: America/New_York

targets:
  - name: Don Angie
    venue_id: 1505                        # see "Finding a venue_id"
    dates: ["2026-11-14", "2026-11-15"]   # any of these is fine
    time_window: ["18:30", "20:30"]       # what you want
    fallback_window: ["17:30", "21:30"]   # what you'd settle for
    fallback_after: 8                     # seconds before settling
    seat_types: ["Dining Room", "Bar"]    # preference order; [] = any
    release: "30 days ahead at 10:00"     # see "Release rules"
    max_deposit_usd: 0                    # refuse slots with a bigger deposit
```

### Captcha venues and `book_mode`

Some restaurants have reCAPTCHA turned on for booking (`check` prints
`captcha: ON` for them). The bot can't complete a booking there, and this
project doesn't try to get around that. For those, set:

```yaml
    book_mode: manual
```

In manual mode the bot still waits for the drop and polls, but the moment a
slot in your window appears it sends an **alert on your Mac and opens that date
on resy.com in your browser**. You click the slot, solve the captcha,
confirm. It keeps watching for the rest of `poll_duration_s` and alerts again
only if the best available slot changes. You need to be at the laptop at
drop time; the bot buys you the seconds of searching, not the click.

`book_mode: auto` (the default) is for venues with `captcha: off`.

### Finding a venue_id

```bash
python -m resybot venue https://resy.com/cities/ny/don-angie
```

Or in DevTools: the `find` request URL has `venue_id=NNNN`.

### Release rules

This is the part that takes legwork and that no code can do for you. For each
restaurant, find out *when* it opens its books. Sources, in order of
reliability:

1. The restaurant's page on resy.com. Many say "Reservations open 30 days in
   advance at 10:00 AM."
2. The restaurant's own site / Instagram.
3. Watch it yourself: check the page at the top of the hour for a few days and
   note when a new date appears.

**Shortcut:** `check` reads Resy's own release timestamp off any open slot and
prints the rule it implies (e.g. `Resy's release stamp suggests: '27 days ahead
at 10:00'`). Run it for a date that already has open tables, and confirm with a
second date if you can.

Supported formats: `"30 days ahead at 10:00"`, `"every monday at 10:00"`,
`"now"` (no drop; for the future cancellation watcher).

---

## 3. Verify the request shapes (20 minutes, once)

`resybot/resy.py` is written from how Resy's private API has historically
worked. Resy can change it any time. Every line that assumes a field name is
marked `# VERIFY`. Before your first live run, check them against real
traffic:

| What | How to see the real one |
|---|---|
| Headers (`Authorization`, `X-Resy-Auth-Token`, …) | DevTools → any `api.resy.com` request → Request Headers. Make sure `resy.py` sends the same names. |
| `/4/find` request + venue block | ✅ verified 2026-10-13 (POST, JSON body; `venue.id.resy`, `feature_recaptcha`). |
| `/4/find` slot shape | ✅ verified 2026-10-24. Seating is the last part of `config.token`; `config.type` is the meal (Brunch/Dinner); fees in `payment`; release time in `market.date.on`. |
| `/3/details` → `book_token.value` | On resy.com, click a time slot (don't confirm). A `details` request fires. Check its Payload and Response. |
| `/3/book` body | **Only do this on a table you actually want.** Confirm a booking on resy.com, find the `book` request → Payload. Compare field names to `resy.py`. |
| `/2/user` → `payment_methods[].id` | DevTools → the `user` request after login → Response. |

If anything differs, edit `resy.py` only. Nothing else needs to change.

---

## 4. Run it

### The easy way: menus

```bash
./resy
```

Arrow-key menus for everything: **Book a restaurant** (pick from the
restaurant list, search all of Resy, or paste a link; then date, times and
Go, with everything else defaulted), **My reservations** (booked, waiting, history;
change / turn off / remove), **Practice run**, and **Settings & Resy login**. Edits are saved
to `targets.yaml`.

- **Runs in the background.** “Start the bot → In the background” keeps it
  going after you close the terminal and keeps the Mac awake (`caffeinate`).
  The menu shows whether it's running; “Bot: watch or stop it” to check in.
  Keep the Mac plugged in with the lid open at drop time.
- **Logs itself in.** Settings → “Log in with email & password” once. The
  password goes in the macOS Keychain (never a file); when Resy's token
  expires the bot logs in again on its own, even mid-drop.

### Classic commands (still work)


```bash
python -m resybot check            # zero risk: token OK? next drops? what's open now?
python -m resybot run              # DRY RUN: full routine, stops before booking
python -m resybot run --live       # the real thing
python -m resybot run --live --only "Don Angie"
```

`check` prints each target's next drop moment. Start `run` any time before
that, leave the terminal open, and **don't let the laptop sleep**:

```bash
caffeinate -i python -m resybot run --live     # macOS
```

Logs go to `logs/resybot.log` (readable) and `logs/resy.jsonl` (every
request and response, token redacted; this is what you read when a drop
fails and you need to see why).

### Recommended first live run

Pick a *mid-demand* restaurant you'd happily eat at, with
`max_deposit_usd: 0`. If it books, great, you have dinner plans. If it
fails, the logs will show where the request shape was off.

---

## 5. Tuning

| Setting | Default | Notes |
|---|---|---|
| `poll_interval_ms` | 250 | Lower = faster but more suspicious. Don't go below 150. |
| `poll_duration_s` | 90 | Most drops are decided in 5 s; 90 catches slow releases. |
| `fallback_after` | 8 | How long to hold out for the preferred window. |

Losing races at the very hottest places? A $5/month VM in a US-East
datacenter (DigitalOcean NYC, AWS us-east-1) is ~20–40 ms closer to Resy than
a home connection. Copy the folder there, run with `nohup` or `systemd`.

---

## Layout

```
resybot/
  config.py    loads + validates targets.yaml
  release.py   "30 days ahead at 10:00" → real timestamps
  resy.py      the ONLY file that talks to Resy   ← fix here when Resy changes
  ranker.py    picks the best slot (pure logic)
  sniper.py    wait → warm up → poll → rank → book → stop
  notify.py    Mac notifications
  watcher.py   books a released date (used when a Resy Notify email arrives)
  resynotify.py  Resy Notify signups (sign up / list / cancel)
  mailwatch.py   Gmail IMAP IDLE listener for Resy Notify emails
  service.py   shared logic for every interface (parsing, checks, rule detection)
  store.py     safe read/write of targets.yaml and .env
  scheduler.py always-on loop behind `serve` and the menus
  tui.py       the ./resy menus
  cli.py       menu / serve / check / venue / run
```
