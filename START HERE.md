# resybot: start here

A bot that books Resy reservations for you:

- **At the drop:** it waits for a restaurant's tables to open and grabs the
  best table in your times within a second.
- **Sold out?** It signs you up for Resy Notify and books the table the
  moment Resy says one opened up.

**Heads-up:** Resy's terms don't allow automated booking, and accounts have
been banned for it. Use it for your own dining, not resale, at your own risk.

---

## What you need
- A Mac
- A Resy account with a card saved (add one in the Resy app under Payment)
- A Gmail account (only for booking cancellations automatically; optional)

---

## 1. Set it up (5 minutes)
1. Unzip `resybot-sharable.zip`, e.g. into your Downloads.
2. Open **Terminal** (Cmd+Space, type "Terminal", Enter).
3. Type `cd ` (with a space after it), drag the `resybot` folder onto the
   Terminal window, and press Enter.
4. Type `./resy` and press Enter.
   - The first time, it sets itself up (a minute or two).
   - If your Mac asks to install **"command line developer tools"**, click
     Install, wait for it to finish, then run `./resy` again.
5. Log in with your **Resy email and password** when it asks. Your password
   goes into the Mac's Keychain, never into a file, and the bot logs itself
   back in whenever Resy's login expires.

Next time, just open Terminal and do steps 3–4 again.

**The first time you open it, a Getting started checklist appears.** Pick
any item to see how to do it (most have a button that does it for you). It
ticks things off by itself and disappears once everything is done. The rest
of this guide is the same steps in more detail.

---

## 2. Book something
Everything is arrow keys + Enter. To go back a step, pick **← Back** or
press **Esc**. Ctrl+C jumps back to the main menu.

1. Pick **Book a restaurant**.
2. Pick the restaurant. Type to filter the list. Restaurants marked
   **captcha** in red need you to click the final button yourself (see below).
   Not on the list? Pick **Search all of Resy** and type any name.
3. Pick the date (it suggests the next release date) and your times.
4. The last screen shows party size, seating and so on. Change anything you
   like, or just press Enter on **✅ Go**.
5. The bot then does whatever fits:
   - **Tables not out yet:** it books the second they drop.
   - **Tables already out:** it books one right away if one's free.
   - **Sold out:** it offers to sign you up for Resy Notify (step 4).
6. When it asks **"Turn on the bot now?"**, say **Yes**.

Try **Practice run** first if you like. It's exactly the same, but it stops
right before booking and tells you which table it would have taken.

---

## 3. Keep the bot on
The bot runs in the background, so you can close Terminal and it keeps going.

- At drop time keep the Mac **plugged in with the lid open**. It stays awake
  by itself while the bot is on.
- Turn it on or off any time: **Settings → Turn on / Turn off the bot**.
- The main screen shows **Right now**: what the bot is working on.
- **My reservations** shows what it booked (with reservation numbers), what
  it's waiting on, and the last week's activity.
- Updated the bot? Turn it off and on again so it uses the new version.

---

## 4. Cancellations with Resy Notify (optional, one-time setup)
For a sold-out date, the bot signs you up for **Resy Notify** (the bell in
the Resy app). Resy emails you when a table opens; if the bot can see that
email, it books the table within seconds. It never checks Resy over and over.

**a. Make sure Resy emails you Notify alerts.** In the Resy app, open your
profile → notification settings and turn on **email** for Notify.

**b. Make a Gmail "app password"** (a separate 16-letter password just for
the bot; your normal Gmail password won't work):
1. Turn on **2-Step Verification**: myaccount.google.com/security. You only
   get asked for the extra step when signing in on a new device, not every
   time you open Gmail.
2. Go to **myaccount.google.com/apppasswords**, type `resybot`, click
   **Create**.
3. Copy the 16 letters Google shows (it's only shown once).

**c. Connect it:** in the bot, **Settings → Gmail for Resy Notify**. Enter
your Gmail address (the one your Resy account uses) and paste the 16 letters.

The app password lives in your Mac's Keychain. The bot only opens emails from
Resy and never marks anything as read. To cut it off, delete the app password
on that Google page.

Skip this step and Notify still works: Resy alerts your phone and you book
the table yourself.

---

## Captcha restaurants
Some restaurants (marked **captcha** in red) have a captcha on the final
step, which the bot can't click. For those, the second a table appears, it
opens the Resy page in your browser and plays a sound. You click the time
and confirm.

---

## If something goes wrong
| Problem | Fix |
|---|---|
| `permission denied: ./resy` | Run `chmod +x resy`, then `./resy` again. |
| "Resy login expired" | Settings → Log in with email & password. |
| "Gmail said the email or app password is wrong" | Use the 16-letter app password, not your normal Gmail password (step 4b). |
| App passwords page says "not available" | Turn on 2-Step Verification first. |
| "Bot is off" on the main screen | Settings → Turn on the bot. |
| Nothing booked at the drop | Check the Mac was awake and plugged in, and the bot was on. My reservations shows what happened. |

More technical detail is in `README.md`.
