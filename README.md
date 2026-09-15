# Lab KPIs

A graph dashboard over the HIS lab reports. It signs in to the hospital portal
with the operator's own account, pulls the *sample received* report over a date
span they choose, and draws the numbers a lab is actually asked about: how many
samples came in, how long they waited before somebody accepted them, and where
the waiting happens.

Sibling of the Pending Authorization dashboard, and it borrows that project's
hardest-won piece — the sign-in — unchanged.

```
HIS :8000  ──login page, filled in by a hidden browser──▶  app.py
HIS :8006  ──POST /api/v1/Authentication/SignIn──────────▶  app.py  (one-hour token,
HIS :8026  ──POST /api/v1/MISManager/GetReportData───────▶  app.py   renewed by itself)
                                                             │
                                                             └──plain HTTP──▶  the page
```

## Signing in

Open the dashboard and the sign-in window is already there: **User Id**,
**Password**, *Sign In*. Nothing else is asked. It closes itself as soon as the
first fetch lands. The `HIS:` chip in the top bar reopens it; it says *live*,
never who is signed in — the board hangs on a wall.

The site is not asked for: everyone here works at the same hospital, so it is
set once in Advanced (default *Almoosa Specialist Hospital*). An account that
does not have that site takes whichever site the portal offers first rather than
stopping the person signing in.

### What happens behind the scenes

The portal scrambles the password **inside the browser** before posting it to
`/api/v1/Authentication/SignIn` — that is what `X-App-Mode: ENCV0` in the token
means — and the key lives in its own JavaScript, so a server cannot build that
request by itself. So the dashboard lets the portal do it: it starts the browser
already installed on the PC **with no window**, opens the real login page, types
the user id and password into the real form, and takes the token out of the page.
Signing in takes a few seconds.

While that happens the portal's own encrypted sign-in payload is captured, so
renewing the hour-old token is just a re-post of it — no browser, no attention.
If the HIS ever refuses the replay (a password change, for instance), the hidden
browser signs in again by itself from the credentials held in memory.

The password is held **in the server's memory only** and is never written to
disk; `data/his_config.json` holds settings, nothing else. After a restart, sign
in again.

**Requirements:** Microsoft Edge or Google Chrome on the PC running the
dashboard — Edge ships with Windows, so normally there is nothing to install. If
the PC genuinely has neither, Advanced settings has a bearer-token box as a
break-glass (F12 → Network → any call → the `authorization` header); it lasts
about an hour and cannot be renewed.

### Settings (Advanced)

Advanced settings are hidden: none of it is the staff's business, and a wrong
value there breaks the dashboard for everyone on that PC. Whoever sets the PC up
gets the panel back by **clicking the sign-in window's title five times**, or by
opening the dashboard with `?advanced=1` (the title works in the app window,
which has no address bar).

| Field | Meaning |
|---|---|
| Login page URL | The page the staff open every morning. Default `https://hisalmoosaprod1.almoosahospital.com.sa:8000/`. |
| Report API URL | HIS REST host. Default `…:8026`. |
| Sign-in API URL | Authentication host — a different port: `…:8006`. Used for the token renewals. |
| Site | The site everyone signs in to. Default *Almoosa Specialist Hospital*. |
| Refresh every | Seconds between fetches (minimum 30; default 300). |
| Browser for signing in | Blank uses the Edge/Chrome found on this PC; set a full path to pick one. |
| Verify the HIS certificate | Turn off only if the HIS presents an internal certificate that the PC does not trust. (The hidden browser then ignores it too.) |

Settings are saved to `data/his_config.json`. Credentials are not.

## Where the data comes from

### The main report

`reports/samples_received.json` is the exact body the portal posts for report
**1094**, *sample received lIST With Service Name* (category *ASH Reports2*),
captured from its own `GetReportData` request. Only two things are filled in per
request: the dates, and `loginUserId` read from the token.

Its `queryType` is 3 with a **null `queryString`** — this report's SQL lives on
the HIS, so unlike the Pending dashboard's report 1106 there is no `.sql` file
alongside it.

The dates go out the way the portal writes them: the staff's local midnight
expressed as UTC, so a report for the 14th is sent as `2026-09-13T21:00:00.000Z`.
The offset is `utc_offset_hours` in the settings (default 3). Sending noon UTC
instead — which is what the Pending dashboard does, because that report truncates
to the day — would put an early-morning or late-evening sample in the wrong day
here.

Columns used: `SAMPLE_NO`, `MRNO`, `PATIENT_LOCATION`, `INV_CATEGORY_NAME`,
`TEST_NAME`, `RECEVID BY NAME`, `DEPARTMENT_NAME`, `ENTRY DATE`,
`COLLECTION_DATE`, `SAMPLE_ACCEPTANCE_DATE`, `SAMPLE_ACCEPTANCE_BY`,
`DEPARTMENT_SAMPLE_ACCEPTANCE_BY`, `SITE_NAME`. They are read by name through
`app.column()`, which forgives the HIS's spacing, so the spelling in
`RECEVID BY NAME` does not have to be corrected to be found.

### Adding another report

Every other `.json` in `reports/` is fetched alongside the main one and **joined
onto it by sample number**. Its columns are added to the sample they belong to,
prefixed with the file's name (`vitals.RESULT_TIME`), so two reports carrying a
`DEPARTMENT_NAME` cannot overwrite each other. A sample a linked report does not
mention is simply left alone, and a linked report that fails does not cost the
rest of the board.

To add one:

1. Run the report once in the portal with F12 → Network open.
2. Copy the `GetReportData` **request payload** (the body only — the headers
   carry a live bearer token and are not needed).
3. Save it as `reports/<name>.json`, blank out `loginUserId` and the two date
   `storedValue`s, and add a `_meta` block:

```json
"_meta": {
  "title": "Results entered",
  "key_column": "SAMPLE_NO",
  "date_filters": { "from": "FROM_DATE", "to": "TO_DATE" }
}
```

`_meta` and `_comment` are notes for whoever reads the file; they are stripped
before the body reaches the HIS.

Nothing else needs changing to *fetch* a linked report. Putting its columns on
the board is a change to `build_kpis()` in `app.py`.

## History

There is no local database. The span the viewer picks is what the HIS is asked
for, so looking further back is a wider query rather than a longer memory, and
every number on the board is the HIS's own. The range buttons go to a year; past
that the report is slower than anyone will wait.

The board opens on `days_back` from `data/his_config.json` — 1 by default, which
is yesterday and today. The range buttons override that for this PC and the
choice is remembered in `data/view.json`, so the setting is what a fresh PC
starts on rather than what it is stuck with.

The cost is that a wide span is a slow fetch. If a year turns out to take too
long to sit behind the same Refresh as a day, the fix is to move the long ranges
onto a button of their own rather than to start keeping a copy.

## The charts

Drawn as plain SVG in the page — no chart library, nothing fetched from a CDN,
which matters on a PC that may not reach the internet at all.

Each chart is one series, so its title names it and no legend is needed. The one
ordered scale is the wait-before-acceptance chart, which darkens with the wait
along a single blue ramp; everything else is the same blue. Status (the tick and
the warning triangle on the tiles) always carries an icon and a word, never
colour alone. **Table view** in the filter row prints every chart as numbers.
Both light and dark are chosen, not flipped.

## Running

```bash
pip install flask
python app.py                 # opens its own window
python app.py --no-window     # plain server on http://localhost:5050, Ctrl-C to stop
```

The dashboard is one person's program, not a site on the network. It listens on
`127.0.0.1` only — another PC cannot reach it — and starting it opens the
dashboard in a window of the PC's own browser: Edge or Chrome asked for a plain
window, with no address bar and no tabs, so it looks like an application rather
than a web page. The window keeps a profile of its own under `data/window`, so it
remembers its size and position.

**Closing that window stops the program.** A reload (F5) does not: the page sends
a heartbeat every three seconds and the server gives a page that has gone quiet
twelve seconds to come back. On a PC with neither Edge nor Chrome the dashboard
falls back to the browser used for links, and then only closing the *page* stops
it, not the browser.

## Building the executable

`BUILD.bat` / `BUILD.sh` produce the single-file executable; `reports/` is
bundled with `templates/` and `static/`.

It is also built for you. **Build the Windows executable** runs on every push
and pull request and leaves `LabKPIs.exe` as a downloadable artifact on the run
— so installing it on a PC is a download, and a pull request can be tried out
before it is merged. The workflow runs `BUILD.bat` itself rather than keeping
its own copy of the flags, so the two cannot drift apart; that script's closing
`pause` is skipped when `CI` is set, which it always is on a runner and never is
when somebody double-clicks it.

Building is not the whole check. A forgotten `--add-data` still produces an
`.exe`, and only shows itself when the thing is started and cannot find its
templates or its report bodies — so the workflow starts the executable it just
built, waits for it to serve its page, and asks it whether it can still see
`samples_received`.

Tagging a version (`git tag v1.0 && git push --tags`) publishes the executable
as a GitHub release.

## Still to do

- **Its own icon.** `static/icon.png` is currently the Pending dashboard's, so
  the two look identical in the taskbar. It wants a mark of its own, and the
  build has no `--icon` yet.
- **The linked reports themselves** — the joining is built and waiting; no second
  report has been captured yet.
