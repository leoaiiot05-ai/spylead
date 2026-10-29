# spylead

spylead measures how many days US leading indicators lead SPY, using only data as it was known at the time. It runs on this PC, and you read its charts in Edge on the PC and in Safari on the iPhone and iPad.

## What it does, and what it does not

It does:

- download SPY's daily closes from Tiingo, and 18 US leading indicators from FRED, 30 years back from the first fetch, plus 3 years to warm up;
- keep every indicator value as it was first published, on the day it was published; later revisions are ignored;
- smooth every series in real time: the curve's value for a day uses only data published by the previous month end (the one-sided, or L1, curves);
- for each indicator, find the shift A, from −730 to +730 days, that makes SPY divided by the shifted indicator (raised to a fitted power β) as flat as possible; indicators whose best A is negative lead SPY, and are kept as `LEADL1_<name>`;
- combine the kept indicators into F, with a lead for each, and show how flat F is, also on data the fit never saw;
- show all of it on a page you can install as an app on the PC, the iPhone and the iPad.

It does not:

- trade, place orders, or connect to a broker (IB Gateway included);
- forecast, or draw anything past today;
- fetch by itself: data comes only when you press Fetch Data;
- work while the PC is off or spylead is not running;
- publish anything: FRED's, ICE's and Tiingo's data are for your own use.

## What you need

- This Windows 11 PC, with the external drive at a fixed letter, E: (Install, step 1).
- Python 3.13, 64-bit, from python.org, with the `py` launcher. The lock file pins packages built for Python 3.13.
- Git for Windows, and Microsoft Edge.
- For the iPhone and iPad: Tailscale on the PC and on both devices, signed in to the same account.
- Two free API keys:
  - **FRED**: create an account at fredaccount.stlouisfed.org, open API Keys, then Request API Key. The key has 32 characters.
  - **Tiingo**: sign up at tiingo.com, then open your account's API page, which shows your token.
  - Both go into `config.toml` only, which git ignores. Never commit them or paste them anywhere else.

## Install

Every command runs in PowerShell, in the project folder once step 2 has made it.

1. **Give the drive a fixed letter.** Right-click Start, Disk Management; right-click the external drive's volume, "Change Drive Letter and Paths…", and choose E:. The autostart relies on it.
2. **Get the code** into `E:\00_CODING\spylead`, unless it is there already:

   ```
   git clone https://github.com/leoaiiot05-ai/spylead E:\00_CODING\spylead
   cd E:\00_CODING\spylead
   ```

   On an exFAT drive, Git refuses the folder until you trust it once:

   ```
   git config --global --add safe.directory E:/00_CODING/spylead
   ```

3. **Create the virtual environment** and install the locked packages:

   ```
   py -3.13 -m venv .venv
   .venv/Scripts/python -m pip install --upgrade pip
   .venv/Scripts/python -m pip install -r pylock.toml
   ```

   pip warns that installing from `pylock.toml` is experimental; that is expected. Every package is pinned with a hash, and nothing else is installed.

4. **Run the tests.** They use no network and take about a minute:

   ```
   .venv/Scripts/python -m pytest -q
   ```

## Configure

1. Copy the example:

   ```
   Copy-Item config.example.toml config.toml
   ```

2. Make a random web token, and paste it as `token` under `[web]`. It is the password of the web page, at least 16 characters:

   ```
   .venv/Scripts/python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```

3. Paste the FRED key as `api_key` under `[fred]`, and the Tiingo token as `api_key` under `[tiingo]`.
4. Keep `port = 8090` unless another program uses that port. spylead refuses the ports of IB Gateway's API.

`catalog.toml` lists the indicators. spylead reads it when it starts, so restart it after an edit.

## Run

```
.\run.bat
```

- The console says `spylead is ready on http://127.0.0.1:8090`. Ctrl+C stops spylead.
- If it says that another spylead process holds `data\spylead.lock`, spylead runs already, perhaps through the autostart: use that one.
- The log is `logs\spylead.log`, and the data is `data\spylead.db`.

Open http://127.0.0.1:8090 in Edge and log in with the web token.

## The first Fetch Data

- The page says there is no data yet. Press **Fetch Data**.
- The status bar follows the job: downloading (SPY, then each indicator), smoothing, measuring leads, fitting F, saving.
- The download takes a minute or two: spylead sends FRED at most two requests a second. The compute then takes several minutes, and must stay under 10 minutes on this PC; the bench below checks that. The charts appear when it ends.
- The first fetch fixes the dates for good: the analysis starts 30 years before that day, and the download 3 years earlier still.

After that:

- Press Fetch Data when new data may be out: SPY after each US trading day, most indicators once a month.
- spylead first asks Tiingo for its latest trading day, and FRED for each series' last update. When nothing is new, it downloads nothing and recomputes nothing, and says the data is **already up to date**.
- When something is new, it downloads only the new observations, then recomputes everything.
- A series whose download fails (a timeout, an HTTP error) keeps its data, and the table shows the error. The others go on.

## Reading the results

### The candidate table

| Column | Meaning |
|---|---|
| Name | The series. A kept one also shows its `LEADL1_` name. Tap a name, or a row, to open its chart. |
| Form | LOG for positive quantities, fitted in logarithms; LEVEL for rates, spreads and indices that can be zero or negative. |
| Last observation | The date the latest value describes. |
| Last release | The day that value was first published. |
| Estimated points | How many points have an estimated release day. FRED's archive of first releases (ALFRED) does not reach back to every old value; those get the value's date plus the series' usual lag. |
| A* (days) | The best shift. Negative: the indicator leads SPY by that many days. |
| β | The fitted exponent: how strongly the indicator enters. Negative: it moves against SPY. |
| Score | How far ln F strays from a flat line, on average. Lower is flatter, which is better. |
| Out-of-sample | The same search on the first 70% of the window only, scored on the last 30%, which that search never saw. Much worse than the score means the fit may be luck. It is shown only, and changes nothing. |
| Status | Why the series is kept or not (below), with the error of the last fetch if it failed. |
| In F | Uncheck a kept series to leave it out of F. The choice is remembered, and new kept series are in by default. |
| Remove | Only for series you added. |

The statuses:

- **Kept**: it leads SPY (A* between −730 and 0, both excluded), and its data is current.
- **Not leading**: the best A is 0 or later.
- **At boundary**: the best A is ±730 days, the edge of the search, so no lead was found in range.
- **Insufficient history**: less than 10 years to compare with SPY. Expected for the two ICE BofA spreads, which FRED keeps for 3 years only.
- **Discontinued**: it would be kept, but no new value has come for much longer than usual. It never enters F.
- **Invalid values**: a LOG series has a value that is zero or negative.
- **Pending**: no data yet. **Fetch failed**: the last fetch failed, and there is no data.

### The charts

- **SPY**: the raw closes (thin grey), SPYL1, its one-sided curve, and the reference fit (dashed). The log axis shows growth rates evenly.
- **The one-sided curve** uses, on each day, only data published by the previous month end: what you could have known then. So it reacts late. The lead analysis uses these curves only.
- **The reference fit** is one curve through all the data, drawn with hindsight. It is for your eyes only: nothing is computed from it.
- **F** is SPYL1 divided by the kept series, each shifted back by its lead b and raised to its β, scaled so that its median is 1. The closer F stays to 1, the better the leading series together follow SPY. The dashed vertical line marks the start of the out-of-sample part. Above the chart: the score, the out-of-sample score, the slope per year, and each member's b and β.
- **SPYL1 and the composite**: the composite is what the members, at their leads, put together for SPYL1. Where the two part, F moves away from 1.
- **Each series**: its first releases as points, its one-sided curve and its reference fit. The kept ones come first; the others are under "Other series".
- **Zoom** with a pinch or the slider, or with Ctrl and the mouse wheel on the PC. 1Y, 5Y, 10Y and All set the range. A tap shows the date and every value of that day.

### Smoothness N

N is the most turning points (inflection points) a curve may have per year, 2 by default. A higher N follows the data more closely and reacts sooner, but also follows noise. Changing it recomputes everything, which takes several minutes.

### Adding a series

Type any FRED series ID (for example INDPRO) under the table and press Add. spylead checks it with FRED, downloads it and recomputes. Annual series are refused. Remove deletes a series you added, with its data.

## The PC: install as an app

In Edge, open http://127.0.0.1:8090, then the "…" menu, Apps, **Install this site as an app**. spylead then opens in its own window, from the Start menu or the taskbar.

## iPhone and iPad: Tailscale Serve

spylead listens on 127.0.0.1 only, so no other device can reach it directly. Tailscale Serve passes it on, over HTTPS, to the devices of your tailnet only.

1. In the Tailscale admin console, under DNS, turn on MagicDNS and HTTPS Certificates.
2. On the PC, once:

   ```
   tailscale serve --bg --https=8443 http://127.0.0.1:8090
   ```

   It keeps serving after a restart; `tailscale serve status` shows it.
3. On the iPhone or iPad, with Tailscale connected, open `https://<pc-name>.<tailnet>.ts.net:8443` in Safari. `tailscale status` on the PC shows the PC's name. Log in with the web token.
4. Share, then **Add to Home Screen**. spylead then opens full screen from its icon.

**Never use Tailscale Funnel, and never open or forward a port to the internet.** Either would put spylead on the public internet, where a single password would be all that guards the page, the PC behind it, and data whose licences allow your own use only. Serve keeps it inside your own devices.

## Autostart

To start spylead whenever you sign in to Windows:

```
powershell -ExecutionPolicy Bypass -File scripts/install-autostart.ps1
```

- It registers a logon task named `spylead`. The task runs `%LOCALAPPDATA%\spylead\launch.ps1`, on the system drive, which waits up to 10 minutes for the external drive, then starts spylead hidden, without a console.
- If Windows refuses the task, the script puts a shortcut in your Startup folder instead, and says so.
- `%LOCALAPPDATA%\spylead\launch.log` records each start, or that the drive never came.
- Run the script again after moving the project. To undo everything:

  ```
  powershell -ExecutionPolicy Bypass -File scripts/install-autostart.ps1 -Remove
  ```

- An autostarted spylead has no console, so Ctrl+C cannot reach it. To stop it, end `pythonw.exe` on Task Manager's Details tab, while no job runs. The database stays whole and the lock is freed; a job you end this way is lost, and the results before it stay.
- Under the autostart, an error in `config.toml` leaves no message anywhere (TODO.md). If the page does not come up, start `run.bat` to see why.

The drive must keep its letter, E:; otherwise the launcher waits for a folder that is not there.

## Backups

Stop spylead, then copy `data\spylead.db` somewhere safe. While spylead runs, its latest changes can sit in `data\spylead.db-wal`, which a copy of the database alone would miss. Keep a private copy of `config.toml` too: it holds your token and keys.

## Checking it on the PC

These are the owner's checks of SPEC.md §16.3:

1. Run the tests (Install, step 4).
2. With both keys set, start spylead and press Fetch Data, and wait for the charts. Press it again: it says the data is already up to date. Compare SPY's latest close with Tiingo's site, and M2's latest value with FRED's.
3. Run the bench. It times one full compute on synthetic data of the real size, in a temporary database; it never touches `data\` and never fetches:

   ```
   .venv/Scripts/python -m spylead.bench
   ```

   It prints the time of each phase. A full compute must take under 10 minutes; the bench ends with exit code 1 when it does not.
4. Walk through the UI checklist in `plans/prompt-09.md`: Edge as an app, an iPhone, and an iPad in both orientations through Tailscale Serve.
5. Install the autostart, sign out and back in, and check that the page comes up once the drive is mounted.

## Limitations

These are deliberate (SPEC.md §17):

1. The one-sided curves react late. Within a month, each day's value follows the previous month end's fit along its slope, so a turn shows only after the next month ends. The leads are measured between these real-time curves, not between the hindsight ones.
2. The reference fit uses all the data, with hindsight. It is for display only.
3. FRED's archive does not hold every old first release. Those release days are estimated, and so are the leads that rest on them.
4. When FRED first publishes an older observation late, spylead skips it: a value counts only if it is newer than everything published before it.
5. SPY is the raw close, without dividends.
6. FRED keeps only 3 years of the ICE BofA spreads, so the CCC and high-yield spreads cannot be analysed. ISM's PMI and the Conference Board's LEI are not available at all.
7. A good fit is not a forecast. With many shifts and exponents fitted to smooth, trending curves, a flat F can arise by chance. The out-of-sample score is the guard, and it is shown only.
8. With several members, other sets of leads can fit almost as well. F's leads are the best the search found, not a unique truth.
9. Nothing is projected past today.
10. The iPhone and iPad reach spylead only while the PC is on and spylead runs, and spylead cannot start without the external drive.
11. The data is for your personal use only.

## Troubleshooting

- **"Another spylead process holds data\spylead.lock"**: spylead runs already. Open the page, or stop that one first.
- **The page says the connection is lost**: the PC sleeps, spylead stopped, or Tailscale is off on one side.
- **A "Missing key" banner**: put the key into `config.toml`, then restart spylead.
- **A series shows "Last fetch failed"**: press Fetch Data later. Its data stays as it was.
- **Nothing at http://127.0.0.1:8090 after signing in**: look at `%LOCALAPPDATA%\spylead\launch.log` and `logs\spylead.log`, or start `run.bat`, which shows any error.

## Files

| Path | What it holds |
|---|---|
| `config.toml` | The web token and the two keys. Git ignores it. |
| `catalog.toml` | The indicators. Restart spylead after an edit. |
| `data\` | The database and the lock. Git ignores it. |
| `logs\` | `spylead.log`, rotated at 5 MB, with 5 old files kept. Git ignores it. |
| `run.bat` | Starts spylead in a console. |
| `scripts\install-autostart.ps1` | Starts spylead at each sign-in, or with `-Remove` stops doing so. |
| `SPEC.md` | The agreed behaviour, in full. |
| `TODO.md` | What is deferred. |
