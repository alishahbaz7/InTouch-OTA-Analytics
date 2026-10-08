# Release notes

Newest first. The in-app version history (`ota_analytics/__init__.py`, shown at `/api/version`)
carries one line per release; this file explains the reasoning.

---

## 2.0.2 — in development

Built on 2.0.1 from the user's review of the Commands page and the job page (08 Oct 2026).

### Commands: laid out for a growing library

- **One search** across both tables — names, codes, commands, tags and notes. While searching,
  each table says how many of its rows match ("1 of 3").
- **A total and an S.No. column** on both tables; the numbers carry on across pages.
- **Paging at 5, 20 or 50 rows**, with the dashboard's own pager; each table pages on its own,
  and keeps the search and the other table's place.
- **The add forms stay folded** under an *Add* button until wanted; *Rename* and *Edit* open
  them filled in. The tables are the page.
- **Grouped by what a command does** — GET, SET, CLR — then by name. Each parameter says how many
  saved commands use it, so a rename says what it will affect. Each saved command has a **New
  job** action that opens a job with it on the first line.
- **Saving a command already saved under another name says so.** Two names can be on purpose, so
  it is not refused. Where one command has two names, the first saved names it everywhere — it
  used to be whichever row the database returned last.

### Sharing a library: export and import

- **Export** writes the whole library as one CSV — `kind, name, command, tags, note` — for both
  parameter names and saved commands, so it opens in Excel and can be sent to a colleague. A
  **template** shows the format.
- **Import** reads such a file and first shows, row by row, what it would do: *new*, *update*
  (and what changes), *same*, or a *problem* with its line number. Nothing is written until
  *Apply*, and an import never deletes — loading someone's file cannot wipe your own names.
- The *From library* picker in New job and Configure is taller, has a scrollbar that can be seen
  on the dark theme, and gets a filter once there are more than eight saved commands. Past six
  commands the list had looked cut off.

### A job takes its devices from one source

A saved group, typed ids or a CSV: whichever is used, the other two grey out and are not sent;
*Clear* empties it and brings them back. Before, all three were open at once and the server
quietly picked one (CSV, then group, then ids). The page is right on its return from a preview
as well, not only while typing.

### The job page, leaner

The time-to-answer chart and the answered-on-attempt bar under the tiles are gone, at the
user's request. The same figures are per command in the Command summary (*First try*, *Time to
answer*) and per device in the device table. They had first been drawn as a column chart that
scaled with its card and filled half a wide screen, then as compact bars — which the user saw
unstyled (next section), and asked to have removed.

### The stylesheet reaches the browser when it changes

Its URL was versioned by the app version — `app.css?v=2.0.1` — so it stayed the same through a
day of style changes on one version, and the browser kept the copy it had. New parts of pages
came out unstyled: bars as plain text ("< 30 s 5"), while every screenshot from a fresh browser
looked right. The URL now carries the file's own time as well (`api.static_version`), so a
changed stylesheet is fetched on the next page load.

A test now also checks that every rule in the stylesheet is whole. Removing the retired chart
styles line by line left half of a two-line rule behind, which the browser reads as the start of
a selector — silently dropping the rule after it.

### Fixed

- The import preview's *Apply* button read "<built-in method update…>": its counts used the key
  `update`, and in Jinja a key named like a dict method is read as the method. Now `updated`,
  and CLAUDE.md lists the names to avoid.

---

## 2.0.1 — 2026-10-07

Built on 2.0.0 after the first live jobs (06–07 Oct 2026).

### Why a device answers "on the third attempt", made visible

On 07-10-2026 the desk device 786 answered about 5¾ minutes after the **first** attempt of every
command, three times out of three. A job waits 2 minutes before resending, so attempts 1 and 2
looked unanswered, and the answer landed on whichever record was newest by then. It was not a
multi-device problem: single-device sends in the same job behaved the same way. The cause is not
settled yet (a device that takes minutes to wake is the leading reading), so the retry rule is
unchanged. What changed is that the next batch will show what is happening:

- **Each answer records which attempt carried it and how long after the first attempt it came**
  (schema **v16**: `cota_campaign_result.answered_attempt`, `answer_seconds`). The job page shows
  time to answer per command (the middle device) and per device, and how many answered on the
  first try. The export has both columns.
- **The wait for an answer is a job setting** (Settings → *Wait for an answer, per attempt*,
  10–3,600 s; 30 s by default — see "Fast and bounded" above). In the simulator, a device that
  answers at 5¾ minutes gets one send with a 7-minute wait instead of three.

### Jobs: a list first, a page to create one

- **Jobs opens on the list.** Each row shows the sequence, a progress bar, answered, failed and
  expired counts, when it started and how long it has run. **New job** (top right of the list)
  opens its own page with the form and the plan. *All jobs* leads back from every job page.
- **The device table uses the user's headers:** S.No. (the device's position in the job, under
  any filter), Device ID, IMEI, State, **Command**, **Now at/Total** (e.g. 3/4), **Attempt**,
  **Answered**, **Failed**, then time to answer and the last answer with its OK / Failed reading.
- **A device unfolds in place** to its conversation in that job: every attempt with its time,
  its tick and what the cloud said ("sent by the cloud, no answer"), and the answer on the
  attempt that carried it. *Open in Configure* is still there for the full thread. Unfolded rows
  stay open while the page redraws.
- **Duplicate** and **Rerun unanswered** fill a new job from an earlier one: all its devices,
  or only those that did not answer every command. Nothing starts until the plan is previewed.
- **Names as you type:** each command line is named under the box before you preview, by this
  install, so the page and the plan cannot disagree.
- While a job runs, the browser tab shows its progress, e.g. "(25%) #3 Test 2".
- On a narrow window the wide tables scroll sideways inside their card.

### Fast and bounded: 30 s per attempt, and no job past an hour

On 07-10-2026 a job of 2 devices × 4 commands — 8 commands in all — was still open after 12
hours. One device's second attempt had been *held* by the cloud for a sleeping device, and the
rule then was never to resend a held command but to wait for it, up to 12 hours. The cloud never
handed it over; the device had talked to the OTA platform three hours later. The user's call:
"not justified — design it so that even in the worst case no job lasts more than an hour."

- **Each command: 30 s per attempt, up to 3 attempts, then the next command** — the user's own
  model, now the rule for jobs and for sequences in Configure alike. No answer in 30 s is no
  answer, whatever the cloud says it did with the command — sent, held, or not listed — so a held
  command is resent like any other. Nothing is added between attempts (the 30 s is the gap);
  the next command goes 2 s after an answer. **Every command is over in about 90 s.**
- **Every job has a time limit**, 60 minutes unless set in its Settings. At the limit, each device
  still in progress stops — its current command *expired*, the rest *skipped* — and the job ends.
  It replaces *Wait for sleeping devices* (12 h). The job page says when the job ends at the
  latest, and the plan states the worst case — how long the job takes if nothing answers — and
  warns when that would not fit the limit (a large group at a low call rate).
- **A late answer is kept.** One that arrives after the device has moved on to its next command
  turns the earlier command from failed into *answered late*. (One that arrives after the device
  has finished the whole job is not waited for — that is the price of a fast job — and still
  shows in the device's conversation on Configure.)
- **Attempts are exactly 30 s apart.** An attempt that runs out is resent in the same 10 s tick
  that notices it, not the next one, with a second of slack for call pacing; without both,
  attempts slipped to 40 s apart.
- A send the cloud *refuses* is retried after 30 s, not at once, so an outage is not hammered;
  three refusals in a row still pause the job.
- 2 devices × 4 commands, with nothing answering at all, now ends in about 6 minutes (tested).

### What happens next to each device, and when

Asked after a live test: 14906 sat at "waiting · attempt 2" with no way to tell when anything
would happen. (Its attempt was held by the cloud, and the rule then waited 12 hours for it — the
rule "Fast and bounded" above replaced.) Nothing on the page said so.

- **A Next column** in the job's device table says what the scheduler will do for that device
  and when, with a countdown that ticks every second and the clock time: *Sends*, *Resends ·
  attempt 3*, *Gives up — next command*, *Gives up — finishes*, *Answered — moves on*, or *Stops —
  time limit* when the job's limit comes first. A paused job says *paused*. The countdown
  runs on the server's clock (the page reads it on every redraw), not the browser's.
- `cota_campaign.next_action` reads the device's own timers by the same rules the scheduler
  runs on, and is tested case by case against them.
- **The resend now comes when the page says it will.** Past the first two minutes a waiting
  device is checked every 60 s, so a 7-minute answer wait could run up to a minute over before
  the resend. The check that ends the wait is now moved onto the moment it ends: the resend is
  the answer wait plus the 30 s guard band, to the scheduler's 10 s tick (tested).

### Jobs at a glance: tiles and graphs

- **A job's progress is tiles and pictures**, replacing the row of small counts. One split bar
  shows the whole job by outcome (answered green, waiting amber, failed red, expired grey; the
  bare track is what is not sent yet). Five tiles give each count with what it means: "81% · 195
  on the first attempt", "no answer after 3 attempts". **Time to answer** is a column chart over
  < 30 s, 30 s – 2 min, 2 – 5 min, 5 – 10 min and 10 min +, with the typical and slowest times
  and the job's resend wait beside them. **Answered on attempt** splits answers into 1st, 2nd
  and 3rd attempt, which says directly when the answer wait is too short.
- **"By command" is now "Command summary"**, with an outcome bar on each row.
- **The Jobs page opens with today's dashboard**: jobs (running, paused), devices reached,
  commands answered (share of finished, first-attempt count), typical and slowest time to
  answer, failed, and cloud calls (sends · checks — the checks are what grow on a big job). Below
  them, **outcomes per job** as one split bar each, and **answers per hour** today.
- Every number comes from this install's record in a few grouped queries — no cloud calls — and
  each command is counted once (`cota_campaign.outcome_segments`): a sent command still waiting
  for its result is *Waiting*, never also *Not sent yet*. The pictures are server-drawn SVG and
  CSS, like the rest of the dashboard, and redraw with the job page every 5 s.

### Commands: a library of names

A new **Commands** page in the Intouch COTA rail:

- **Parameter names:** `6C0A` → a name of yours. Every GET, SET and CLR of that parameter then
  reads by it everywhere — Configure, Jobs, the plan, exports. A built-in name (FTP_SETTINGS,
  SOS) can be renamed and reset.
- **Saved commands:** a whole command under a name, with tags and a note. A saved command's name
  is shown wherever that exact command appears. **From library** adds it to a new job;
  **Library** in Configure fills the command box, or adds a line to a sequence.
- Kept across days, like groups and the device map (schema v16: `cota_parameter`,
  `cota_saved_command`). `cota.describe_command` reads the names from memory, loaded once per
  database and refreshed on every change.

### Icons

Every new control draws its icon from the app's own line-icon set through one macro
(`_icons.html`), sized by the control it sits in, at the existing type sizes. No new font sizes.

---

## 2.0.0 — 2026-10-06

### IntouchCOTA, a separate module

COTA (configuration over the air) is a different surface from everything before it. The rest
of the app reads the OTA platform's device inventory and works out what happened by comparing
snapshots. COTA *sends* configuration commands through the cloud's own API
(ctvms IntouchAdminApi) and records each request and reply. It has its own tables (schema v10, v11),
its own device map from IMEI to the cloud's internal device id, and its own credential, and it
shares nothing with the snapshot warehouse. Bundles do not carry it.

This is a major version because it is the first time the app writes to a production system
rather than only reading from one. The schema also moves to v10.

### One sidebar for three modules

The header tabs are replaced by a sidebar modelled on the CAN utility's workbench. Each module is
a group and each page an item: **Web FOTA** (everything that existed before), **Web COTA**
(listed, dimmed and marked *Soon*, because it is not built yet) and **Intouch COTA**. The top bar
names the page, the module and what the page is for. The sidebar folds to an icon rail, the
choice is remembered, and on a narrow window it is always a rail that flies out on hover.
`nav.py` is the one definition the sidebar, the top bar and the tests all read.

FOTA's fetch chips and *Update data* button are shown only on FOTA pages. On a COTA page they
would claim a freshness that has nothing to do with what is on screen. COTA pages show their own
sign-in chip instead. The fleet digest stays in every footer.

### Intouch COTA: Configure — a conversation with one device

The first step of COTA is the simplest whole loop: **one device, one command, and what the
device said back**. *Configure* is the first page of the module, laid out as a conversation:

- **Start configuration** with the cloud's device id, or an IMEI the device map knows. An IMEI
  the map does not know is refused rather than guessed. Devices already configured are listed
  with their last command, newest first.
- **The thread** shows commands sent as bubbles on the right, marked *Accepted by cloud* or
  *Not accepted* with the reason. What the cloud returned shows on the left, headed
  `Device-<IMEI>`, with day separators. Replies that read as failed (`FAIL`, `ERROR`, …) or
  as OK are badged. This is a reading of the device's own words, labelled as one; failure wins
  a tie, so `KEEP,FAIL,KEEP FAILED` is not read as fine.
- **The composer** takes the command type and value (val2–val9 when needed). The exact JSON
  that will go is shown as you type, which stands in for a separate preview step. Recent
  commands are one-click chips. Sending answers with a redirect, so refreshing the page cannot
  send a command twice.
- **Watching for the reply.** The cloud does not push replies, so after a send the page checks
  every 10 seconds for three minutes, with a live *Waiting for reply · 0:42*. It stops when a
  new entry arrives. *Check replies* asks at any time.
- **What Web FOTA knows**, beside the thread: online or offline at the last fetch, last seen,
  model, firmware, config. It says up front whether a reply is even possible; an offline device
  holds the command until it next connects. It is the registry's single row for the IMEI, so it
  costs no snapshot resolution.

Each device's console is its own job (`Console · device <id>`): every send is a task and every
check a `cota_poll` row, so nothing new was added to the schema and all of it appears under
Jobs. A rejected token is recorded on that command instead of stopping a job, so nothing is
left planned to go out by surprise with the next send.

### Configure: ticks, the IMEI, and the cloud's record of each command

A real `getGPRSCommand` record (captured 2026-10-06) settled what the cloud returns: **one record
per command sent to the device**, from any source, with its own id
(`865510083360422_6AC48E16`), the send `timestamp` in epoch seconds, the device's `imei`, a
`status`, and `response` / `responseTime`, which are empty until answered. It is not a list of
replies. The console is now built around that:

- **Ticks, like a messenger.** ✓ *command via API*: the send call answered
  `{"msg":"Command send Successfully.."}`. White ✓✓ *response via API*: `getGPRSCommand`
  returned the command's record (`status` 0, `response` null). Green ✓✓ *response via device*:
  the same record now carries the device's answer in `response`, with `status` 1. All of this
  was confirmed on the live cloud and matches the portal's own COTA screen, where *Pending
  Count* counts status 0. ✗ in red when the cloud refused the send; 🕓 while sending. After a
  send the page keeps watching until the device answers, saying which side it is waiting for.
- **The device's answer is shown as it came**, control bytes and all: `\x16`-style codes rather
  than garbled characters. The cloud gives no `responseTime` even after answering, so the time
  shown is when this page first saw the answer, and it never moves on a later check.
- **Counts like the portal's** *Total / Pending*: total, waiting for the device, answered, plus
  not-yet-in-the-cloud and not-accepted when there are any.
- **Command names**: type 36 is *Zenithra Command*, as the portal calls it
  (`cota.COMMAND_NAMES`); other types show their number.
- **Matching a send to its record.** The cloud rewrites `val1`, so values cannot be compared.
  A record belongs to a send when the device and type are the same and its timestamp is within
  2 minutes. Closest pairs decide which belong together, and time order decides which goes
  with which: closest-first alone crossed two quick sends over, giving the newer send the older
  one's answer. Replayed on the live records for device 14906, both sends paired correctly. Records with no send of ours were
  sent from the portal or another install, and appear outlined as *sent elsewhere*, so the
  thread is the device's whole history.
- **"ID: 14906 | IMEI: 865510083360422".** The IMEI comes from the device map, or from the
  first cloud record, and is then saved into the map (source *cloud record*). That lights up
  the Web FOTA card with no manual upload. An IMEI a person already mapped is never overwritten.
- **Model 124 and type 36 are locked defaults**: pre-filled and sent, changeable only with
  *Edit*.
- **Click a command** for what was sent from this page, the cloud's full record (rewritten
  `val1` and all) with Copy, and the three stages with their times. Hover shows the record too.
- The page stops checking on its own once the latest command has its cloud record.

### Configure, cleaned up

- **The thread is command → answer**, nothing between. The tick legend and the "response via
  API" bubbles are gone; each tick explains itself on hover, and the waiting line under the
  newest command says who it is waiting for.
- **Raw data lives in a side panel**, folded by default and remembered. Clicking a command *or*
  its answer opens it on **Raw**: the stages with times, the raw request (`saveCOTAConfig`),
  the send reply, and the cloud's raw record, each with Copy. A **Device** tab holds what Web
  FOTA knows; the header keeps one line of it — *● Online · last seen 4 hr ago* — so the fact
  that matters stays in view while the panel is folded. Below 1800px wide the device list
  folds to avatars while the panel is open, so the thread keeps a readable width.
- **⟳ refresh** replaces *Check replies*, spinning while it works. After a send the page checks
  three times, 30 seconds apart (*Auto-check 2/3 · in 0:18*), stops early once the device
  answers, and then leaves it to ⟳.
- **A time range**, `DD-MM-YYYY HH:MM` in 24-hour time, typed or picked from the calendar,
  defaulting to today 00:00–23:59, with *Today / Yesterday / Last 7 days*. It drives both what
  the cloud is asked for and what is shown, and lives in the URL. The browser's own date-time
  box was not used: it follows the PC's locale and can show 12:00 AM. Ranges are capped at 15
  days because the cloud's limit is unknown. After a send the start stays and the end moves to
  the end of today, so the new command is always in view.
- **Export** the conversation in the range as Excel or CSV: one row per command, what was sent
  beside what came back, control bytes written as `\xNN` exactly as on screen. Columns: IMEI,
  Device ID, Sent at, Value sent, Cloud val1, Cloud status, Response via API seen, Response via
  device, Answer seen. The workbook's
  *Source* sheet names the device, the range and the counts.

### Sequences: one device, several commands, none missed

**Configure → Sequence** takes commands one per line and sends them in order, in the
background, by rules agreed with the user:

- **One at a time.** The next goes only once the current one has an outcome, so every answer
  belongs to exactly one command.
- **2 s after an answer, a guard band after anything else.** Once the device has answered, the
  next command goes 2 s later. Resending the same command, and moving on after one that gave
  up, wait the guard band: **30 s, 60 s for an unrecognised command** — three of the device's 10 s
  heartbeats. The cloud is checked every 10 s, a command not in the cloud's list after 30 s
  never arrived, and an attempt waits 2 minutes for an answer.
- **Up to 3 attempts, then the next command.** A resend never duplicates by accident: it is
  always safe when the earlier attempt provably never reached the device (refused, or never
  listed by the cloud); once it may have, a GET (`DA…`) or SET (`DB…`) can go again — repeating
  it changes nothing — and so can a CLR (`DD…`, e.g. `DDD76D66` CLR SOS), by the user's decision,
  knowing a resend minutes later could clear a new SOS raised in between. Only an unrecognised
  command is never resent once it may have arrived. Commands show readable names built from
  their parts — `GET FTP_SETTINGS`, `CLR SOS`, `SET 6C0A`. A late answer to an earlier attempt still
  counts.
- **It pauses rather than fails** when the session cannot be renewed or the cloud stops
  answering three calls in a row, and resumes from the same attempt. Runs live in the database
  (schema **v13**: `cota_run`, `cota_run_step`), so closing the page changes nothing, and a run
  interrupted by a restart comes back paused, saying so.
- A command that is not a command — not hex, an odd length, not `XX D7 …` — is refused before
  anything is sent. While a sequence is live, sending by hand is blocked.

`tests/test_cota_run.py` drives the runner against a simulated device and cloud that act out
every failure case — asleep, API down, missed reply, never received, wrong command, late answer,
an expired session mid-run, pause, resume, cancel — using the user's own five commands. Time is
simulated, so every wait is its real length and the whole matrix runs in about a second.

### Jobs: many devices, the same sequence each

**Jobs** sends a sequence of commands to many devices, up to the whole fleet (23,103 today,
sized for 30,000), and shows where every device is.

- **Choose the devices** from a saved group, by typing ids, or by uploading a CSV in the cloud's
  own device-list format, `id,trackingCode`. `id` is required and `trackingCode` (the IMEI) is
  optional. **Download template** on both upload forms gives the format. Tracking codes in an
  upload are added to the device map. **Groups** are saved on the Devices page and kept across
  days. Saving under an existing name replaces the group.
- **Preview before anything is sent.** The plan shows each command by name, the canary, how many
  send and check calls the job will take, and roughly how long. A bad command, a device already in
  a live job or sequence, or no cloud session blocks *Start*.
- **Each device moves through the sequence by the Configure rules**: 2 s after an answer, a
  30 s guard band, up to 3 attempts, no accidental duplicates. A scheduler ticks every 10 s and
  sends one command to every device ready for it in one call per batch (default 50 devices). It
  paces calls to the job's rate (default 5 a second) and checks only devices with something
  outstanding, less and less often while they sleep.
- **A sleeping device is not sent the command again.** The cloud holds the command until the
  device wakes. The device is waited for until the job's validity runs out (default 12 h), then
  marked expired.
- **It starts small and stops itself.** Jobs over 20 devices send to a canary first (1%, between
  1 and 20 devices). A job pauses itself after three failed calls in a row, or when more than 10%
  of finished commands fail. Resuming after a canary stop releases the rest. A refused call (for
  example, the session expired) is not counted as an attempt.
- **The job page** shows progress, a command × outcome grid, and every device with its state,
  current command, attempt and answers, filtered and searchable. Each device links to its own
  conversation on Configure. While the job runs, the page redraws every 5 s from this install's
  record, so watching it costs the cloud nothing. Pause, resume and cancel are on the page, and
  **Export** writes every device × command to CSV (streamed) or Excel.
- **One job at a time**, and a job that was running when the app stopped comes back paused.
- **The COTA record lives for the day.** The first COTA page opened on a later day clears the
  earlier days' jobs, sequences, sends and cloud records. Groups and the device map stay.
  Single sends from Devices are listed on Jobs too, since that page links there.
- Schema **v14** (`cota_group`, `cota_group_member`, `cota_campaign`, `cota_campaign_device`,
  `cota_campaign_result`) and **v15** (`ix_cota_task_device`). Without that index a 5,000-device
  job took 303 s of scheduler time; with it, 48 s.

**What decides how long a large job takes: checks, not sends.** A check (`getGPRSCommand`) reads
one device per call. On the live cloud, a comma list of ids got HTTP 400. 30,000 devices × 5
commands, simulated end to end (`OTA_SCALE_DEVICES=30000`): **162 send calls, 152,072 checks**,
every command answered. At the default 5 calls a second that is about 8½ hours. Raising the rate
is the lever, but the rate the cloud tolerates is not yet known. The plan states this beside its
estimate.

### Tests can no longer reach the network or the real credential store

Writing those tests, one of them started a real background run, and a real run uses the real
cloud client and the token in Windows Credential Manager: it most likely made **one live
request** — `DAD76C0A`, a GET from the user's own list, to the desk device — which the expired
token probably made the cloud refuse. Now every test runs with HTTP refused unless it fakes the
cloud itself, an empty in-memory credential store, and no real background runs.

### "Command sent at …" is delivered, not answered

On 2 of the first 7 live commands the cloud set `status` 1 and wrote `Command sent at
2026-10-06 16:05:09` into `response`. That is the cloud saying it *delivered* the command, not the
device answering, and the console showed it as a green ✓✓. It is now its own stage, **delivered ·
no answer from the device** (white ✓✓), counted as waiting for the device. Rows stored before
the fix are read the same way.

### Username and password sign-in, and a token that renews itself

The portal's own sign-in request was captured (2026-10-06): `POST
IntouchAdminApi/user/login`, multipart form, `username` and `password` with the password sent
as its MD5 hash — the same scheme as Web FOTA's. The InTouch cloud preset now carries all of
it, locked like the API URL, so signing in with a username and password just works, and nothing
posted can redirect the password elsewhere.

With the password remembered, an expired token no longer stops anything: on a 401 the tool signs
in again by itself and repeats the step once — a check simply asks again; a refused send, which
never reached the device, is sent once more, with the refused attempt kept in the record. A
token from the environment is never renewed over, because the environment would still win.

### An expired token shows on the cloud chip

When the cloud answers 401 or 403, the chip turns red — **"Cloud: session expired"**, with the time
and HTTP code in its tooltip — the moment it happens, including during a Refresh without a
reload, and the page stops checking on its own. The message box and the Sign in page say the
same, with a link. It clears itself when a fresh token is saved or the cloud next accepts the
held one. The message itself no longer tells people to run `cota token set`, a command that was
never built.

### Configure, tidied

- **The page no longer scrolls.** Top bar, console and footer together fill the window exactly,
  so the side strip, the header and the composer stay put; only the conversation (and a long
  device list) scrolls.
- **One surface, split by a line**: the device list and the conversation share a frame with a
  visible divider between them, as in a messenger. Finding why it was missing turned up a bug:
  retiring an old style earlier had cut half of a shared rule, so both panes had lost their
  background and border and the conversation had taken the list's padding. A test now fails
  if one rule names the same selector twice — the shape that mistake left behind.
- **The check status says when, not how many**: *Checked 16:59:10*.
- **The composer is one line**: the value and Send. *More values (val2…val9)* and the live
  *Sends {…}* preview are hidden for now — type 36 takes one value, and the exact request is in
  the Raw panel once sent. The send route still accepts val2…val9.
- **Model and type left the composer.** They are set from **Edit** beside "ID: 14906 | IMEI: …";
  they are still sent with every command, a pill beside the name shows when they are not the
  defaults, and a changed command type survives the reload after a send.

### The whole conversation for the period, as the cloud holds it

`getGPRSCommand` returns every command for the device in the window asked — from this tool, the
portal or anyone else — with its answer. The console now mirrors exactly that:

- **Opening the page, or choosing a new range, loads that period from the cloud**, once. It is
  skipped when the same period was loaded within the last minute. Until now it asked only after
  a send or on Refresh, so a period this copy had never asked for looked empty. The header
  says what it holds: *Loaded 16:45 · 6 records in the cloud for this period*.
- **A command that leaves the cloud is shown as such.** A record stored from an earlier check
  that is in the window but missing from a later, complete answer — deleted in the portal, most
  likely — is marked *no longer in the cloud since 16:47*, faded and counted, rather than shown
  as current. It is unmarked if it reappears. An empty `data` list counts as an answer; a body
  with no list at all marks nothing. Schema **v12** adds `cota_command.missing_since`.
- **Cloud sign-in is the status chip.** The separate *Cloud sign-in* button beside
  "Cloud: signed in" did the same thing and is gone; the chip opens Sign in and is highlighted
  while you are there.

### Tests can no longer touch this machine's data

A new test opened the database without pointing it at a temp file and migrated the dev copy's
real database to v12. It was harmless: one empty column, which the dev copy would have added on
its next start, with every command and snapshot intact. But the guard written after it found
**seven older tests that had been opening the real database all along** — page renders in
`test_auth.py` and `test_startup.py`. Now every test runs with all of `data/` (the database,
both connection settings, scheduler state, the error log) redirected to a temp folder, and
opening either live database (`data/` or `dist/…/data/`) fails the test outright.

### Buttons you can see, and a heading that stays put

- The console's actions were dim outlines on the dark theme, and three of them blank: a running
  copy had new templates but older code without their icons. They are now named buttons —
  **⟳ Refresh**, **⤓ Export**, and **Raw** / **Device** on the side strip (clicking the open one
  folds the panel; **Fold** inside it does too) — with full-colour text and a visible border.
  A test fails if a template asks for an icon that does not exist.
- **The dev copy now says "Restart to load new code"** when Python files on disk are newer than
  the code it is running. Templates reload by themselves and code does not; that mismatch had
  already shown up as 500s, an empty command table and blank icons. Never shown by a release.
- Scrolling the page made the thread's heading paint over the top bar. A rule meant for the top
  bar was written against every `<header>` element, so the heading was sticky too. The rule is
  now `header.topbar`, and a test keeps it that way.

### Scrollbars and native controls follow the theme

Reported as "not acceptable at all", rightly: a white scrollbar on the dark theme. The page
never told the browser its colour scheme, so everything the browser draws itself —
scrollbars, dropdowns, date pickers — came out light. Every theme state now declares
`color-scheme`, and scrollbars everywhere are thin and drawn in the theme's own colours.

Schema **v11** adds `cota_command`: the cloud's records, upserted by their id so checking again
never duplicates, with `first_seen_at` marking when each reached the white ✓✓.

### Intouch COTA: Jobs, Devices, Sign in

- **Jobs** lists the jobs held in this install (it became the many-device page; see above).
- **Devices** sends a command, and loads the IMEI → cloud device id map from a sheet, which
  it searches and pages. **Send a command** takes exactly the fields of the portal's request:
  device model (`deviceType`), device ids (`deviceList`, comma separated), command type
  (`type`) and its values (`val1` … `val9`). Or paste the portal's payload, including the
  `^"`-escaped *Copy as cURL (cmd)* form, and *Read payload* fills the form. It takes three
  explicit steps because this writes to real devices. Read and Preview touch no network, and
  Preview shows the exact JSON of every call. Send needs a ticked box naming the device count,
  and is refused if the form no longer matches the preview. A typed command becomes a job like
  any sheet does, so it is sent and recorded by the same code (`cota.send_job`) and appears
  under Jobs with the cloud's reply. Lists over 50 devices are split into calls of 50. Over 200,
  or for a sequence, use Jobs.
- **Sign in** is the filled button top-right, where FOTA keeps *Update data*, beside the
  "Cloud: signed in / not signed in" chip. It chooses the cloud and how to get a token. There are two ways, as on the FOTA
  connection: paste the token from the portal, which works today, or sign in with username and
  password. The cloud's login call has not been captured yet, so its URL is entered rather than
  guessed. Sending a password to an address nobody verified is the one thing this must not do.
  Tokens and passwords live in the credential store or the environment, never in
  `cota_connection.json`. The COTA password has its own account name, so it cannot overwrite the
  FOTA password for the same user.

### The release and a source copy run side by side

Both used to default to port 8000. A dev launch then found the release answering there, decided
the app was "already running", opened the release and exited, so a code change looked like it
had not taken effect. Each copy now has a **channel**: `release` when packaged, `dev` from
source, or set by `OTA_CHANNEL`. Everything the two could fight over is keyed on it:

| | release | dev |
|---|---|---|
| Default port | 8000 | 8100 |
| Defers to a running copy of | release only | dev only |
| Session cookie | `ota_session` | `ota_session_dev` |
| Marked in the UI | — | **DEV** badge, "DEV ·" in the tab title |

The cookie matters because browsers keep cookies per host, not per port: with a password set,
signing in to one copy would have signed you out of the other. The databases were already
separate, because each copy writes beside itself. A server deployed from source is a release in
every sense but packaging, so the deploy templates now set `OTA_CHANNEL=release`.

---

## 1.9.1 — 2026-10-05

### One release, named for its version

The release is now `InTouchOTA-Analytics-v1.9.1.zip`, and it unpacks to a folder called
`InTouchOTA-Analytics-v1.9.1`. Previously the zip was `InTouchOTA-Analytics-v1.9.0-win64.zip`
and unpacked to an unversioned folder, so two releases side by side were indistinguishable once
unpacked. This is the naming for every release from here on, in this project and the user's
others — the shared `ship` skill now says so.

Because each release unpacks to its own folder, upgrading by unzipping now means moving the old
`data` folder across before the first start — otherwise the new copy opens on an empty database.
`READ ME FIRST.txt` says so, since it is the only instruction a recipient of the zip gets. The
working folder in `dist\` is deliberately left unversioned: this install runs from there and keeps
its database inside it, and a folder renamed every release would leave the history behind.

The executable itself keeps its unversioned name, so shortcuts and pinned icons survive upgrades.

### The silent executable is gone

`InTouchOTA-Analytics-silent.exe` existed for one reason: auto-start ran it after a reboot so no
console window was left on the desktop. Auto-start was withdrawn in an earlier release, so nothing
launched it any more — and anyone who did run it by hand saw nothing at all, which reads as a
program that failed to start. The release now carries one executable.

`startup.launch_command()` still prefers a windowless build when one is present, so bringing
auto-start back means adding it to the spec again and nothing else. A silent exe left in `dist\`
by an older build is cleared as build output rather than "rescued" as the user's file.

---

## 1.9.0 — 2026-10-05

Built on 15 September and released three weeks later, during which the install kept running
1.8.0 and got worse in exactly the way measured here. On 5 October, opened after five days
offline, a single fetch sat in "Updating the device registry" for more than eight minutes. Re-
measured that day on a copy of the live database (362 snapshots, 760 MB): the one statement it
was stuck in took **360 seconds**, and the post-gap fetch had stored 22,564 changed rows against
~3,500 for an ordinary hourly one — so a gap multiplies every per-change cost on top of a
resolution that had grown to 18s. The same scenario on 1.9.0, with an even larger gap fetch
(34,995 rows): **26.9 seconds**, most of it the first round of retention catching up.

### A fetch took three minutes; it takes about ten seconds

Reported from the live install after a month of hourly fetching: "fetching and ingesting data
taking very long time, sometimes application get hanged, I have to restart the application",
and "I try to export the data this is not working".

Measured on a copy of the live database — 574 MB, 272 snapshots, 1,081,179 change rows, 35,848
devices — one fetch took **194 seconds**, and 453 on a second run. The dashboard's own
`snapshot.duration_ms` shows the slide: 2.7s in August, 27–53s by September, and that column
stops counting before the slowest step even starts.

It was four separate problems.

| Step of one fetch | Before | After |
|---|---|---|
| Storing what changed | 9.0s | **1.6s** |
| Checking data quality | 25.9s | **1.0s** |
| Updating the device registry | 150.6s | **4.7s** |
| Rebuilding metrics | 8.7s | **0.5s** |
| **Total** | **194s** | **~8s** |

**SQLite had no query statistics, and guessed catastrophically.** `ANALYZE` had never run on
this database. Without it the planner chose `ix_change_field` over `ix_change_imei` for the
registry's `prev_firmware` update, so for each of 35,848 devices it scanned all 21,097 change
rows with `field = 'firmware'` — 756 million row visits, **367 seconds in a single statement**,
on every fetch. `ANALYZE` takes 1.2 seconds and makes the same statement take **0.21s**. It runs
once in migration v9, and `PRAGMA optimize` keeps it current after every ingest.

This was the cheapest fix in the project's history and it hid for a month, because the wrong
plan still produces the right answer.

**`device_state` was being resolved eighteen times per fetch.** It is a view, and SQLite plans
it as a full scan of `device_snapshot` with a correlated subquery per row, so one reference
costs time proportional to *all history stored* — 4 to 18 seconds here, growing by another fetch
every hour. The quality rules resolved it once per rule; the delta write twice; rollup twice
more.

The new `resolve.py` does the same resolution as a grouped join over `ix_ds_imei_snap`:
**17.5s → 1.3s** for all 27 columns of all 35,848 devices, byte-identical output on nine
snapshots including the first and the last. It cannot be a view — the snapshot id has to bound
the inner `GROUP BY` and SQLite has no LATERAL — and two other formulations turned out slower
than the view rather than faster (`ROW_NUMBER() OVER (PARTITION BY imei)` at 17.4s, restricting
the view's join at 12.5s).

The view stays in the schema. It is the readable statement of what resolving a snapshot *means*
and it is what `tests/test_resolve.py` checks the fast path against.

**Pages are unaffected**, and that was verified rather than assumed: 1.8.0 and 1.9.0 were run
side by side against identical copies of the live database, interleaved so machine load hit both
equally. Every page and every download came out within noise of each other. (Wall-clock on this
machine swings 2–3× depending on what else is running, which is exactly why the comparison had
to be interleaved.)

### One fetch at a time

`database is locked` appears twice in the live error log, both times at the very first INSERT of
a scheduled fetch. A timed fetch ran outside `progress` entirely, so "one job at a time" did not
cover the one job that runs by itself — pressing "Fetch now" during a scheduled fetch started a
second concurrent writer, and one of them waited out the 30-second busy timeout and died.

The timed fetch now takes a job like everything else, so the two cannot overlap in either
direction. A tick that finds something already running **skips** rather than queueing: the next
tick is along shortly and its data is fresher. A skip is recorded and explained but not counted
as a failure — an agent that appears to have silently stopped fetching is the thing that gets
debugged for an afternoon. A job nobody started also clears itself when it finishes, so Update
Data does not open on a stale panel every hour.

### Retention could never fire, so the database grew for ever

Retention thins by age, and also refused to prune any snapshot that recorded a change. That
sounds careful. At the hourly cadence the tool actually runs at, a fetch of 35,848 devices
always contains *some* change — so **268 of 272 snapshots were exempt** and retention removed
nothing, on every run, while the database grew about 45 MB a day.

The rule is gone, because it protected against a loss that cannot happen: `device_change` has no
foreign key to `snapshot`, nothing outside `registry.apply_snapshot` reads its `snapshot_id`, and
every reader works from `changed_at`. Pruning now moves the change-log rows onto the survivor
along with the device rows, so the log stays consistent and the time a firmware move actually
happened is untouched.

On the live data this thins **177 of 273 snapshots and 587,211 of 1,081,179 device rows**, and
every surviving snapshot was verified to resolve byte-identically before and after.

An automatic prune is capped at 20 snapshots (`retention.AUTO_PRUNE_LIMIT`) because it holds the
write lock inside a fetch: the first run after upgrading has a month of backlog, which in one go
is another minute of the app apparently hanging. The rest is taken by the next few fetches. The
CLI's `prune` stays unbounded, because somebody is watching it.

### "Export is not working" — the bundle

Nothing raised, and nothing was ever logged, which is why this was hard to place. A full history
is ~960,000 rows of JSON and the better part of a minute to write, and it was assembled whole in
memory before a single byte reached the browser. As a plain download that is a request which
does not come back: no progress bar, no download indicator, nothing to tell it apart from a
hang.

**Building the bundle is now a job.** The POST returns in milliseconds, a determinate bar counts
the rows as they are written, and the finished job offers a link — collecting the file is then
instant, because it already exists. `GET /update/bundle` still builds one inline for scripts and
short histories, through the same builder so the two cannot drift.

**Nothing large is assembled in memory any more.** The bundle streams to a scratch file (deleted
after sending, and on the error path too). The resolved baseline inside it, which was most of
the export's time, went from 49s to **0.74s** on the fix above.

`exports.to_xlsx` now uses openpyxl's write-only mode: a full device export went from **190 MB
of Python objects to 3 MB** for the same 3.1 MB file, at the same speed. The trade is that
nothing can be revisited after it is written, so column widths, the frozen header and the filter
range are computed up front and the IMEI text format goes on each cell as it is created — all
four are now covered by tests, because all four would have failed silently.

### So it does not happen again

The pattern behind every one of these is the same, and it is worth naming: **nothing errors, the
numbers stay correct, and the work just takes longer every week.** There is no exception to find.

- `tests/test_db_performance.py` traces the SQL actually executed and fails if an ingest,
  rollup, registry fold, quality run or page metric resolves the view. It watches execution
  rather than grepping source, because naming `device_state` in a SQL literal is correct —
  `resolve.at` swaps the table name onto the finished statement.
- `tests/test_resolve.py` holds the fast resolver against the view on every snapshot, including
  a device that leaves and returns and a snapshot that stores no rows of its own.
- `tests/test_retention.py` asserts that pruning never changes what a surviving snapshot
  resolves to.
- Two project skills, `.claude/skills/db-performance` and `.claude/skills/long-jobs-ux`, carry
  the rules and the measurements into the next session. Both of these mistakes had by then been
  made in three separate places each, which is what a rule written only in prose gets you.

### Also

- `python -m ota_analytics.cli vacuum` reclaims file space and refreshes statistics, replacing
  the `python -c "...VACUUM"` line the docs used to carry.
- The scheduler's `last_status` gains `skipped`.

---

## 1.8.0 — 2026-09-08

### Switching pages no longer takes half a minute

Reported from the live fleet at 245 snapshots: moving between Dashboard, Changes and Devices
"creates huge delay in loading the data, which is not acceptable". Measured before the fix, and
it was not one problem but four.

| Page | Before | After |
|---|---|---|
| Overview | 18.4s | **0.84s** |
| Devices | 41.5s | **0.25s** |
| Pending | 76.8s | **0.45s** |
| Firmware | 7.0s | **0.28s** |
| Changes | 1.4s | **0.64s** |

**The newest snapshot is now kept resolved.** `device_state` is a view, and every reference
re-resolves each device's most recent row across the whole fleet — 4.5s at 245 snapshots, and
growing by another fetch every 15 minutes. It was already materialized once per request into a
temp table, but a request opens its own connection, so that 10.9s rebuild was paid on *every
page load*. It now lives in `device_current`, advanced when a fetch lands: ~150 upserts rather
than re-resolving 35,848 devices. `device_current_meta` records which snapshot the rows resolve,
and readers use them only when that matches what they asked for — anything else falls back to
the view, which is slower and always right.

**Three reads bypassed all of that.** They returned correct answers, so nothing failed; they
just each cost a full fleet resolution. `rollup.fragmentation` is called by `kpis()`, so every
page carrying the headline numbers paid it, and *both* queries behind the devices table paid it
again — which is where 41 seconds came from.

**"Pending across several fetches" was counting snapshots.** Three consecutive pending snapshots
meant three days at the original daily export; at the 15-minute cadence it means 45 minutes, so
26,481 devices — three quarters of the fleet — qualified, and the query grouped `device_state`
across all 245 snapshots to work it out (118s). It now reads the change log, which already
records when each device became pending: **0.043s**, and the threshold is `STALL_HOURS = 24`,
because an hour is an hour whatever the cadence.

Two attempts were measured and thrown away: a `valid_to` temporal column made the isolated query
15x faster and the pages no better, and a first rewrite of the stalled query came out *slower*
than what it replaced (SQLite preferred an index that made every pending device scan all 51,178
queue-state rows).

### Fallbacks: how many times, not just when

A chronological list hides the devices that do this repeatedly — on the live fleet, 186
fallbacks across 148 devices, but **23 of them had done it more than once** and one had done it
six times, none of which is visible reading events in date order.

- A **Times** column on every occurrence, and a **Falling back repeatedly** table, worst first.
- The list **pages at 25/50/100** — it previously rendered a hard-sliced 100 rows with no control
  and no indication there was more.
- **Filter** by repeats-only or by model, and **sort** by IMEI, times, model or date. Every view
  is a plain link, so a filtered list is a URL that can be handed to a colleague.
- The download carries **Fallbacks (times)** too — a fallback read away from the dashboard gives
  no hint that it is the device's sixth.

One definition of "fallback" backs the list, the counts, the totals and the file, so they cannot
drift apart.

### A field the source never sent is no longer treated as empty

The platform API carries no group information at all. Treating that absence as NULL wiped the
groups of 29,384 devices on the first API fetch and every one after it — and group is one of the
few dimensions available for explaining why a set of devices reverted. It also made every device
look changed at the moment the source switched, writing 35,475 rows to record nothing.

### Fixes

- The stylesheet is versioned (`app.css?v=…`). Without it the browser caches the file under a URL
  that never changes, so a restyled page keeps rendering with the old rules — which is
  indistinguishable from the new CSS being broken, and is exactly how a set of filter controls
  shipped looking like raw browser links.
- A test now parses every template and fails if a class has no rule in the stylesheet. It would
  have caught that, and the `.num` that should have been `.n`.
- The sortable-column header is a shared macro, so tables cannot each invent their own.

---

## 1.6.0 — 2026-08-19

### Devices page filters

- **Firmware is a checkbox list, not a single choice.** Looking at the two versions a rollout is
  moving between used to take one page load each. The list shows version numbers only — the
  device counts were the widest thing in it and are already on the Firmware page. **All** is a
  real toggle: ticking it selects every version, clearing it deselects every one, and it shows
  the indeterminate dash on a partial selection rather than reading as unchecked while half the
  list is ticked.
- **The Group text box is gone** — an exact-match field nobody could type from memory. The
  capability stays: the Groups page still links here with `?group=…`, carried in a hidden field
  so it survives paging.
- **Find IMEI** takes any part of a number. Digits are extracted rather than required, so a value
  pasted from the platform — quotes, trailing comma — works without tidying, and the last few
  digits off a label are enough.

### Firmware moves is paged

25 / 50 / 100 per page, with the count reporting the **whole** result rather than the rows on
screen, so a filtered view cannot be mistaken for the full one. The pager is a shared macro, so
the remaining long lists can adopt it without a fourth copy drifting.

Ordering is `changed_at DESC, id DESC`. The tie-break is not cosmetic: a snapshot stamps every
change it observes with the same time, so ordering by the timestamp alone lets one row appear on
two pages and another on none. Verified on the live fleet — 1,414 moves walked across 57 pages,
zero duplicates, zero losses.

### Task state by model

Restructured to read like the Firmware table, and now **adds up two ways**:

```
Completed + Pending + No task  = Devices     what the platform was asked to do
Online + Offline + Act-Pending = Devices     whether the device can be reached
```

`Activation-Pending` is a column here and a marker on the Firmware table. It is zero on five of
six rows, so the objection to a mostly-empty column applies — but without it the `(unknown)` row
reads 0 online and 36 offline out of 513 and loses 477 devices with nothing to explain them. Six
rows can afford it; a hundred cannot.

There is deliberately no Task figure under Devices: that number is Pending, and printing it twice
under two headings invites the reader to look for a difference that cannot exist.

---

## 1.5.0 — 2026-08-19

### Page switching was slow. It was measured, not guessed at

`device_state` is a view: every reference re-resolves each device's most recent row across the
whole fleet. At 48 snapshots that is **1.0s per reference**, against 0.01s for a plain count off
the physical table — and a page calls six to ten metrics, each of which referenced it.
`metrics.snapshot_source()` now resolves the snapshot once per request into a temp table.

| Page | Before | After |
|---|---|---|
| Overview | 12.71s | **3.31s** |
| Firmware | 5.16s | 2.52s |
| Devices | 4.33s | 4.42s* |
| Reachability | 3.04s | 1.42s |

\* Devices did not improve; its cost is the row query, not the metrics.

Safety comes from what was *not* changed: every metric keeps its own `WHERE snapshot_id = ?`, so
a caller asking for a snapshot other than the one held gets nothing rather than the wrong rows,
and a metric still reading the view is merely slow. The temp table lives in SQLite's temp schema,
so building it takes no write lock — a read path stays a read path.

Pending is still the slowest page. Its cost is `registry.stalled_devices`, which spans **all**
snapshots and so cannot use the materialized copy.

### Progress for long jobs

A merge takes minutes and a fetch tens of seconds. Both used to hold the POST open with nothing
to show, so the only feedback was a page that never finished loading — indistinguishable from a
hang, and reported as exactly that. Now the POST starts a job, returns, and the page polls.

- The bar is **determinate and derived from work done** — snapshots folded, devices read — never
  from elapsed time. A bar that advances on a timer teaches people to ignore it precisely when it
  matters.
- Steps are **weighted by measurement**: replaying the change log is ~60% of a merge, so even
  weights would leave the bar apparently stalled right through it.
- Progress lives on the **server**, so closing the tab does not stop the job and reopening the
  page finds it mid-flight.
- One job at a time; they all write to the database.

### A build can no longer delete data

`dist/` is cleared to start clean, and an install that lives there keeps its database, bundles and
reports in it. That cost 48 snapshots of live fleet history, more than once. Warning first was
tried and was not enough — the warning scrolled past and the data went anyway.

`build.py` now declares the four files it produces and treats **everything else in `dist/` as the
user's**, moving it aside and putting it back after the zip is written, so the handover file
cannot carry a database either. Listing what to *delete* rather than what to keep is the point: a
kind of file nobody anticipated is preserved by omission instead of destroyed by it.

### One vocabulary, one palette

Nothing is called "stuck" or "failed" any more. The platform assigns tasks in bulk to devices that
are switched off, so a pending task is *parked*; the word carried a judgement the data does not
support, and the page coloured it red like a fault.

| State | Colour |
|---|---|
| Task completed | green |
| **Task pending — Online** | **yellow** — reachable and still not updated, the actionable one |
| Task pending — Offline | orange — waiting by design |
| No pending task | grey |

`Activation-Pending` replaces the platform's `Inactive`: those devices carry an IMEI and nothing
else — no VIN, no ICCID, no first ping, never tasked — so they are waiting to be activated, not
inactive in the sense of having gone quiet. On the live fleet the two descriptions select an
identical 645 devices, so it is a rename of a defined state rather than a new heuristic. **The
stored value stays `Inactive`**, because that is what the export said; only the reading changes.

### Smaller

- **One freshness chip** instead of two: `Updated 09:06 · 3 hr ago · auto 1 hour`. They answered
  the same question and side by side read as two unrelated clocks.
- **Theme switch** — auto / light / dark, applied by an inline script before first paint so there
  is no flash of the wrong theme. "Auto" is a real third state, not the absence of a choice.
- **Pinned table headers**, with the fleet total moved from the foot of 103 rows into the header.
  `top: 0` alone was not enough: the page header is itself sticky at top 0, so the column names
  slid underneath it and vanished exactly when they became useful.
- **XLSX downloads work again.** 128 devices carry an ICCID with an embedded backspace, and a
  `.xlsx` may not contain those characters at all, so openpyxl refused the whole workbook over one
  cell — which is why only CSV appeared to work. A quality rule now reports the affected devices
  rather than the corruption being quietly cleaned away.
- **`.csv` loads as well as `.xlsx`**, columns matched by name. A CSV that turns out to be a report
  this dashboard produced still loads but is marked second-hand, because its values have already
  been normalized once and it drops columns the platform sends.
- **Launching the app twice** opens the copy already running instead of quietly starting a second
  one on another port with a second scheduler behind it.

---

## 1.4.0 — 2026-08-19

Devices-per-firmware gains online, offline and task-pending columns under a grouped header that
names each percentage's denominator. Three columns headed "Share (%)" would have put one word on
three different meanings in adjacent columns.

## 1.3.x — 2026-08-17

CSV ingest; uploads and merges no longer freeze the dashboard; the interleave option asks the
question it means; the connection form says when a password is already saved.

## 1.2.x — 2026-08-16/17

Packaged Windows application — portable, data beside the .exe, CLI reachable from the executable.
"Start with Windows" withdrawn: it produced a copy of the dashboard with no window that could not
be seen or stopped.

## 1.1.0 — 2026-08-16

Fleet digest and database identity on every page and report; snapshot bundles so two installs can
merge history and prove they hold the same data.

## 1.0.0 — 2026-08-15

Device registry with last-checked/last-changed, change log, fallback detection, auto-fetch,
error log.
