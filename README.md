# Lab KPIs

A graph dashboard over the HIS lab reports. It signs in to the hospital portal
with the operator's own account, pulls the *sample received* report over a date
span they choose, and draws the numbers a lab is actually asked about: how many
samples came in, how long they waited before somebody accepted them, and where
the waiting happens.

Five more reports are fetched alongside it and joined on by sample number, which
is what the four KPI tabs — rejections, critical results, and turnaround split
between the work the lab ran itself and the work it sent on — are built from.

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
disk; `data/his_config.json` holds settings, nothing else. (`data/cache` does
hold report rows once the disk cache is on — see *Waiting for the HIS*.) After a restart, sign
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
| Refresh every | Seconds between fetches (minimum 30; default 300). A fetch that takes longer than this is never started again before it has finished. |
| Fetch in chunks of | Days per request when the span is wider (default 7). `0` asks for the whole span in one request. See *Waiting for the HIS*. |
| Chunks at once | How many chunks are in flight together (default 3, maximum 8). Higher is faster here and slower for everyone else on the HIS. |
| Wait for the HIS | Seconds before one chunk is given up on (default 240). |
| Remember fetched chunks | The disk cache. On by default. It writes report rows to this PC — see *Waiting for the HIS*. |
| Keep a remembered chunk for | Days before a cached chunk is fetched again anyway (default 30). |
| Clear remembered chunks | Empties `data/cache` now. The button says how much is in there. |
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

### The linked reports

Five more reports are fetched alongside the main one and joined onto it by
sample number:

| File | HIS report | What it adds |
|---|---|---|
| `specimen_collection.json` | 961, SAMPLE COLLECTION REPORT SPECIMEN | What the sample was drawn into — specimen type, container — and how urgent the collection was |
| `stat_tests.json` | 1044, Total test as STAT with ACCEPTANCE | Sorting and result-entry stamps per test, so turnaround past acceptance reads off the same row |
| `result_time.json` | 593, LAB Result Time | Collection, acceptance, sort and authorisation times, plus the ordering provider |
| `sample_rejections.json` | 957, SAMPLE REJECTION STATUS | Why a sample was turned away, and who collected it |
| `critical_results.json` | 362, Critical\_Results\_New | Whether the sample carried a critical result, and when it was authorised |

Every one of them costs its own pass over the span, so a year is six reports'
worth of waiting, not one. They are cached and chunked exactly like the main
report, so the second look at a span is the cheap one.

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
before the body reaches the HIS. Four more keys are optional, and each exists
because one of the five reports above needed it:

- **`date_filters`** — the report's own spelling of its two date parameters.
  They are not the same everywhere: report 593 calls them `From Date` and
  `To Date`, report 362 just `FROM` and `TO`.
- **`key_column`** — where that report writes the sample number. Two of them
  call it `LIS_SAMPLE_NO` rather than `SAMPLE_NO`.
- **`match_column`** — `{"linked": "TEST_NAME", "main": "TEST_NAME"}`. The main
  report has a row per *test*, not per sample. Without this, a linked report
  that is also per test lands its last row on every test of that sample — the
  urea result's timestamps shown against the blood gas. With it, the two sides
  have to agree on the named column as well as the sample.
- **`skip_columns`** — columns that are true of one row rather than of the
  sample. Report 593 has a row per analyte with its own `RESULT`; joining that
  by sample would pick one analyte's number and show it as the sample's.
- **`fan_out`** — for a mandatory filter with no "all" option. The rejection
  report must name one hospital, so it is fetched once per hospital and the
  answers are put together, with the hospital that answered carried into a
  column of its own.

Every joined sample also gets a `<name>.ROWS` column: how many of that report's
rows belonged to it. One is the ordinary case; more means the other columns
describe the first of several — two critical results on one sample — rather than
the only one. Where the dashboard cannot tell which row belongs to which test,
it counts the disagreement instead of picking silently, and the count is printed
after each fetch.

Nothing else needs changing to *fetch* a linked report. Putting its columns on
the board is a change to `build_kpis()` in `app.py`.

## The tabs

Five boards over the same fetched span. The **Overview** is the original one:
samples received, how long they waited, and where. The other four are one linked
report each.

| Tab | Built from | What it measures |
|---|---|---|
| Rejections | 957, `sample_rejections` | Samples the lab turned away, as a count and against the samples received, and every reason it recorded |
| Critical results | 362, `critical_results` | `MACHINE_RESULT_TIME` → `SECOND_AUTH_DATETIME`: average, median, and how many took longer than fifteen and thirty minutes |
| Referred TAT | 593, `result_time` | `SAMPLE_ACCEPTANCE_TIME` → `AUTHORIZATION_DATE` for samples the lab sent on |
| In-house TAT | 1044, `stat_tests` | `TAT_SORT_TO_RESULT_ENTRY` — sorting to result entry, already worked out by the HIS — for samples the lab ran itself |

Each tab reads its report's **own rows** rather than the columns joined onto the
samples. A rejection or a critical result is a row per analyte and the join keeps
only the first of them (see *Adding another report*), which describes the sample
but not each test on it.

### How a tab knows which department a row belongs to

The other five reports carry no department of their own, so every row is given
the department of **its sample number** — the same way the Lab Analytics app
labels its KPI tabs. The lookup asks two reports in order:

1. the **main report**, 1094, which is where `DEPARTMENT_NAME` comes from and
   which is the spelling the Department filter uses;
2. the **collection report**, 961, for any sample the first one has never heard
   of.

The second one matters, and is why this is not simply a filter on the samples
received. **A rejected sample is often never accepted**, so it never reaches the
list of samples received at all — take the tabs' rows and keep only those whose
sample is on that list and the rejections tab quietly loses the very rows it
exists to count. The collection report still has those samples, because they
were collected before anybody turned them away.

A row whose sample **neither** report mentions keeps its place and is counted as
unmatched: the tiles include it, and a line under them says how many there were.
It only drops out when a department has been picked, because then there is no
honest way to say it belongs — and that line says how many were left out for
that reason too. The same line says how many samples the collection report
filled in, and says plainly when the collection report came back with no
department column at all, which is the one case where nothing can be filled in.

The Department filter reaches all five tabs.

### In house or referred

The main report carries two departments: `DEPARTMENT_NAME`, the department the
sample belongs to, and `DEPARTMENT_SAMPLE_ACCEPTANCE_BY`, the one that accepted
it. The same on both means the lab ran the test itself; a different one means
the sample was referred.

The two TAT reports do not name tests the same way the main report does, so the
lookup is tried twice: exactly, by sample and test, and then by sample alone —
and only when every test on that sample went the same way. A sample either
department is blank on says nothing either way. Rows that could not be placed
are counted and printed under the tiles rather than quietly folded into one side
or the other, along with rows whose turnaround the HIS left blank (report 1044
returns those as `" Hours  Minutes  Seconds"`, which is not a turnaround of
zero).

### Expected times

Both TAT tabs measure each test against its own expected time, typed into the
**By test** table at the foot of the tab and saved to `data/targets.json`. One
list, shared by both tabs; a test nobody has set a time for is held to 60
minutes. That is what the *Met expected time* tile counts.

### Filtering by test

Each tab keeps its own test picker, because each is a different report and they
do not spell a test the same way — `SERVICE_NAME` on the rejections, `TEST_NAME`
on the STAT report, the analyte rather than the test on the critical results.
The names offered are the ones that tab's rows actually contain, and choosing on
one tab leaves the others where they were. Filtering costs no fetch: it is the
rows already in memory being read again.

One figure is withheld under a test filter — the rejection **rate**. Its
denominator is every sample received, which is not the same population as one
service's rejections, so the tab says so rather than showing a number that looks
right and is not. Even across every test it is rejections measured *against* the
received workload rather than a share of it: a sample rejected before it was ever
accepted is in the numerator and, by the same token, not in the denominator.

## History

There is no local database. The span the viewer picks is what the HIS is asked
for, so looking further back is a wider query rather than a longer memory, and
every number on the board is the HIS's own. The range buttons go to a year.

## Waiting for the HIS

The live HIS answers **one day of the main report in about two minutes, and a
month in about ten**. A year asked for in one request does not come back at all:
the report times out, and the board looks broken rather than busy. Three things
between them make a year possible.

### Chunks

A span wider than *Fetch in chunks of* is not one request. It is cut into chunks
of that many days and the chunks are asked for separately, so every individual
request stays inside a length the HIS will actually answer.

Two details matter. The chunks **share their boundary day** — the 1st–8th, then
the 8th–15th — because nothing here knows whether the report counts its
`TO_DATE` as inside the range; overlapping costs one repeated day per chunk,
which is thrown away by the dedupe, while a gap would silently lose one. And the
boundaries sit on a **fixed grid of whole chunks since the epoch**, not on the
dates the viewer happened to pick, which is what makes the cache below worth
having: today's year and tomorrow's year differ only at their two ends.

The rows arrive on the board as their chunk lands, so a year fills in rather
than showing nothing for an hour. The strip under the date buttons says which
chunk it is on, and says plainly when what is on screen is only part of the
range.

### Chunk size tuning

A chunk that times out anyway is **halved and tried again**, down to *chunk_min_days*
(one day), and the narrower size is then applied to every chunk still to come —
so one slow patch of the year costs one wasted wait, not fifty. A chunk that
times out at one day is a real error and is reported as one: the HIS is too slow
for that day, and the answer is a longer *Wait for the HIS* or a narrower range,
not more splitting.

*Chunks at once* fetches several of them together. The HIS spends those two
minutes working rather than talking, so three at a time is roughly three times
the span in the same wall clock — and also three times the load on a report
server the whole hospital shares, which is why it defaults to 3 and stops at 8.
Set it to 1 for a strictly sequential fetch.

### The disk cache

**This is the one place the dashboard writes patient data down.** Everything
else here — the password, the token, the fetched rows — lives in memory only and
goes when the program closes. The cache does not: it keeps each fetched chunk's
rows under `data/cache`, gzipped, in a folder created readable only by the
account running the dashboard. It is what makes the *second* year cost only the
days since the first.

A chunk is reused only when it is asking the same question: the same report over
the same two dates, built from the same report definition — the definition
file's own bytes are hashed into the key, so editing `reports/samples_received.json`
retires its cache rather than serving rows the new definition would not have
asked for.

And only when it has stopped changing. A sample received on Monday can be
accepted on Thursday, and that acceptance rewrites Monday's row, so **any chunk
touching the last two days is always fetched again**, however recently it was
written. Beyond that a chunk is trusted for thirty days. The cache is capped at
512 MB and the chunks nobody has read for longest go first.

Turning *Remember fetched chunks* off in Advanced stops it being written at all,
and *Clear remembered chunks* empties it. If that trade is not one this hospital
wants to make, turn it off: the chunking above still works without it, and a
year is still a fetch that finishes — just every time.

## The charts

Drawn as plain SVG in the page — no chart library, nothing fetched from a CDN,
which matters on a PC that may not reach the internet at all.

Each chart is one series, so its title names it and no legend is needed. The one
ordered scale is the wait-before-acceptance chart, which darkens with the wait
along a single blue ramp; everything else is the same blue. Status (the tick and
the warning triangle on the tiles) always carries an icon and a word, never
colour alone. **Table view** in the filter row prints every chart on the
Overview as numbers. Both light and dark are chosen, not flipped.

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

`BUILD.bat` / `BUILD.sh` produce the single-file executable; `reports/` is
bundled with `templates/` and `static/`.

## Still to do

- **Its own icon.** `static/icon.png` is currently the Pending dashboard's, so
  the two look identical in the taskbar. It wants a mark of its own, and the
  build has no `--icon` yet.
- **A per-report switch.** Six reports is six passes over the span, and there is
  no way to turn one off short of taking its file out of `reports/`.
