# Data Profile — Phase 0 Audit

Source: `Sample data/Devices_35477_15Aug26_1511.xlsx` (22.8 MB, one sheet `data`)
Profiled: 2026-08-15 · 35,477 data rows × 14 columns

This is the Phase 0 gate result. **Read this before designing any metric.**

## Verdict

The export is a **device inventory snapshot**. It answers *"what state is the fleet in right
now?"* It does not answer *"what happened during the rollout?"*

| Needed for | Present? |
|---|---|
| Fleet firmware distribution & fragmentation | ✅ yes |
| Reachability / staleness by firmware & model | ✅ yes |
| Pending OTA workload (queue depth) | ✅ yes |
| Update coverage — devices never targeted | ✅ yes (QUEUE `-`) |
| Inferred task outcome (completed ± firmware change) | ⚠️ by diffing ≥2 snapshots |
| Group / batch composition | ✅ yes |
| Adoption curve, upgrade velocity | ⚠️ only by diffing ≥2 snapshots |
| Per-device upgrade / downgrade history | ⚠️ only by diffing ≥2 snapshots |
| Stalled devices | ⚠️ approximated: queue > 0 across N snapshots with no firmware change |
| Campaign success/failure rate | ❌ not available |
| Failure taxonomy (codes, stages) | ❌ not available |
| Retry counts, attempts-to-success | ❌ not available |
| Update duration percentiles | ❌ not available |
| Bandwidth / bytes transferred | ❌ not available |

**❌ means permanently unavailable, not "later."** The platform owner confirmed (2026-08-15) the
platform stores the same data as this export — there is no event log behind it. These metrics
would require new device/platform instrumentation.

## Columns

| # | Column | Nulls | Distinct | Notes |
|---|---|---|---|---|
| 0 | IMEI | 1 | 35,476 | Natural key. 1 null → 1 row unusable as device key |
| 1 | STATUS | 0 | 3 | Online / Offline / Inactive |
| 2 | QUEUE | 0 | 3 | Pending OTA task count. `0` = 22,070 → **13,407 devices have work queued** |
| 3 | Device Name/VIN | 1 | 35,460 | Mostly a copy of IMEI; `Ax1sCAN` appears 13× |
| 4 | Created By | 0 | 6 | `riya` = 35,344 (99.6%) — low analytical value |
| 5 | Device Model | 0 | 8 | Needs canonicalization, see below |
| 6 | FIRMWARE | 0 | 103 | The core dimension |
| 7 | CONFIGURATION | 8 | 19 | Config version, tracked like firmware |
| 8 | SEEN AT | 0 | 29,145 | `DD-MM-YY HH:MM:SS`; 644 are `-` (exactly the Inactive devices) |
| 9 | ICCID | 35 | 34,749 | SIM identifier; 682 are `-` |
| 10 | hwVer | 0 | 9 | Hardware revision — good segmentation axis |
| 11 | vin | 3 | 9,641 | **`DL1CAB1234` appears 24,512× — a placeholder, not a real VIN** |
| 12 | Groups | 0 | 5,291 | Comma-separated multi-value; 6,093 are `-`. Must be exploded |
| 13 | First Ping | 0 | 32,592 | Device commissioning date; 1,251 are `-` |

## Distributions

**STATUS** — Online 18,874 (53.20%) · Offline 15,959 (44.98%) · Inactive 644 (1.82%)

**Device Model** (8 raw values, ~5 real models)

| Raw | Count | % |
|---|---|---|
| LOCAT140VB | 24,457 | 68.94% |
| TML_Ax1 | 10,225 | 28.82% |
| `-` | 681 | 1.92% |
| AX1_SCAN | 58 | 0.16% |
| AX1_sCAN | 35 | 0.10% |
| AIS-140 | 16 | 0.05% |
| AX1_IOCL | 3 | 0.01% |
| sCAN_AX1 | 2 | 0.01% |

`AX1_SCAN`, `AX1_sCAN`, `sCAN_AX1` are the same model under three spellings (95 devices total).
Canonicalize to `AX1_SCAN`; keep the raw value alongside.

**FIRMWARE** — 103 distinct. Top: `7.5.0.51A` 18,159 (51.19%), `2.0.0161` 4,805 (13.54%),
`2.0.0162` 2,730 (7.70%), `7.5.0.27` 2,139 (6.03%), `2.0.0125` 1,628 (4.59%), `7.5.0.49A` 1,162
(3.28%), `7.5.0.40` 819, `-` 674, `7.5.0.40A` 629, `7.5.0.29` 602, then a long tail of 73 more
values covering 99 devices.

Firmware families are model-specific and use **different versioning schemes** — do not compare
across models:

- `LOCAT140VB` → `7.5.0.x` (+ optional letter suffix: `51A`, `49C`)
- `TML_Ax1` → `2.0.0xxx` (4-digit build)
- `AX1_SCAN` → `5.1.x`
- `AX1_sCAN` → `5.00.xx.xxR` (27 distinct across 35 devices — heavily fragmented)
- `AIS-140` → `7.x.x`, sometimes `V`-prefixed (`V7.2.2` vs `7.2.2` — same version, two spellings)

**CONFIGURATION** — `2.2.2` 19,478 (54.90%) · `1.1` 5,348 · `1.2` 4,850 · `2.1.0` 2,742 ·
`2.2.0` 1,454 · `2.0.7` 693 · `-` 674 · 12 more.

## The finding that matters most: reachability collapses on old firmware

STATUS × FIRMWARE, top 12 versions:

| Firmware | Devices | Online | Online % |
|---|---|---|---|
| 7.5.0.51A | 18,159 | 16,435 | **90.5%** |
| 2.0.0161 | 4,805 | 1,106 | 23.0% |
| 2.0.0162 | 2,730 | 720 | 26.4% |
| 7.5.0.27 | 2,139 | 38 | **1.8%** |
| 2.0.0125 | 1,628 | 18 | **1.1%** |
| 7.5.0.49A | 1,162 | 326 | 28.1% |
| 7.5.0.40 | 819 | 10 | 1.2% |
| `-` | 674 | 0 | 0.0% |
| 7.5.0.40A | 629 | 7 | 1.1% |
| 7.5.0.29 | 602 | 105 | 17.4% |
| 2.0.0139 | 534 | 3 | 0.6% |
| 2.0.0137 | 458 | 1 | 0.2% |

Devices on the current `7.5.0.51A` are online 90.5% of the time; devices two or more versions
back are online 1–2%. Two readings, and the data alone cannot separate them:

1. **Benign** — old-firmware devices are decommissioned/parked hardware that stopped reporting
   long ago, and the fleet has simply moved on.
2. **Serious** — devices are going dark *during or after* an update attempt, and the low online
   rate is the symptom of a failed rollout.

Distinguishing these is the highest-value question in the project. `SEEN AT` age (does the
device's last contact predate the rollout window?) plus a second snapshot resolves it. Build
this comparison into the dashboard as a first-class view, not a buried chart.

## The pending queue is a waiting room, not a backlog

**Operating model, confirmed by the platform owner (2026-08-15):** tasks are assigned in bulk,
deliberately including devices that are not currently reachable. A pending task is not a failure
— it is parked until the device comes back. A device sits pending for one of two reasons:

1. **Powered off / out of service.** It will update whenever it next comes online.
2. **Powered on but unable to reach the OTA platform.** It is pinging, so it looks healthy, but
   the update cannot be delivered.

These look identical in the platform UI, which is precisely why this project exists. They are
completely different operationally: (1) is normal and self-resolving, (2) is a real fault that
nobody is currently able to see. **Separating them is the dashboard's primary job.**

Measured after ingest, once `QUEUE` semantics were confirmed. Of 35,475 ingested devices:

| Task state | Devices | Online now | Not seen 30d+ |
|---|---|---|---|
| completed (`0`) | 22,070 | **79.8%** | 9.6% |
| pending (`1`+) | 7,859 | **2.0%** | **85.1%** |
| never tasked (`-`) | 5,546 | 20.0% | 47.7% |

Devices with a pending OTA task, broken down by how long they have been dark:

| Last ping | Devices | Share of pending |
|---|---|---|
| online now (<24h) | 155 | 2.0% |
| 1–7 days | 256 | 3.3% |
| 7–30 days | 761 | 9.7% |
| 30–90 days | 1,505 | 19.2% |
| **90+ days** | **5,180** | **65.9%** |
| never pinged | 2 | 0.0% |

Mean time since last ping among pending devices: **180 days** (max 775).

**6,687 of 7,859 pending devices (85%) have not been heard from in over a month** — the
powered-off population, waiting as designed. The number itself is not alarming; what matters is
that it swamps the signal, since a single "7,859 pending" figure hides everything actionable
inside it.

**The 155 pending devices that are online right now are the ones that matter.** They are
pinging, so they are powered and connected, yet their task has not completed. That is reason (2)
above: a delivery problem, not a power problem. This is the smallest number in the dataset and
the most operationally urgent — and it is currently invisible.

Two derived measures make this rigorous, both needing a second export:

- **Stuck-while-reachable** — pending, online, and still pending in the next snapshot. Today's
  155 is a single-moment reading; a device online across two snapshots with a task that never
  completes is a confirmed fault.
- **Return rate** — of devices dark for N days, what fraction come back online per week. This
  turns "6,687 devices are waiting" into "≈X will return this month, the rest are effectively
  retired", which is what makes the pending number decision-useful rather than just large.

## Devices never targeted are mostly correct, not missed

1,107 devices are online and have never been assigned an OTA task — 1,105 of them TML_Ax1 on
`2.0.0161` (of that version's 4,805 devices, 4,801 were never tasked).

**Confirmed by the platform owner: this is intentional.** That model has no further firmware
releases; the devices are already running the correct version, so there is nothing to assign.

The lesson is that `never_tasked` cannot be read as a coverage gap on its own. A device is only
a gap if it is **off-target**, which means the system needs an explicit notion of the correct
firmware per model. Without it, every end-of-life fleet looks like a rollout failure. See
`firmware_target` in the schema — target versions are declared, not inferred, because "most
common version" would wrongly mark an in-progress rollout's laggards as correct.

## Data-quality issues to encode as rules

| Rule | Severity | Evidence |
|---|---|---|
| `-` used as null marker across 6+ columns | high | 681 models, 674 firmware, 6,093 groups, 1,251 first pings |
| Device model spelling variants | medium | `AX1_SCAN` / `AX1_sCAN` / `sCAN_AX1` |
| Firmware `V` prefix inconsistency | medium | `V7.2.2` vs `7.2.2` on AIS-140 |
| Placeholder VIN | medium | `DL1CAB1234` on 24,512 devices (69%) — treat as null |
| Null / duplicate IMEI | high | 1 null; 35,476 distinct over 35,477 rows |
| Missing ICCID | low | 35 null + 682 `-` |
| Inactive devices have no `SEEN AT` | info | 644 = exactly the Inactive count; consistent, not a bug |
| `Created By` is single-valued in practice | info | `riya` 99.6% — exclude from segmentation |

## Open questions for the platform owner

With no event log available, the meaning of the columns we *do* have carries the whole project.
Questions 1–3 are now the highest-value unknowns in the system.

1. ✅ **`QUEUE`** — **answered 2026-08-15.** `-` = no task ever assigned · `0` = assigned and
   completed, nothing pending · `1`+ = that many tasks pending. The `-` vs `0` distinction is
   the single most valuable thing learned about this dataset: it separates *never targeted*
   from *targeted and finished*, which is what makes update-coverage analysis possible.
2. ✅ **`STATUS`** — **answered 2026-08-15.** `Online` = pinged within 24h · `Offline` = no ping
   for >24h · `Inactive` (`-` in the UI) = never pinged. STATUS is therefore a recency bucket
   over `SEEN AT`; ingest cross-checks them and flags disagreements (only 3 devices in the
   sample, so the definition holds).
3. **`Groups`** — are these rollout batches? Names like `Chakan_7.5.0.40A`, `49A 7k`,
   `7.5.0.51A 8K`, `All 1824 7.5.0.40A` look like campaign targeting cohorts. If so, groups are
   the closest thing to a campaign dimension available without DB access, and they become a
   primary axis.
4. **Export cadence** — can the platform export on a schedule (daily) to a folder we watch?
   Snapshot frequency directly caps the resolution of every trend chart, and with no event log
   to fall back on, it is the only lever that exists. Manual daily downloads are fine to start.

*(Question 5, database access, is closed — the platform holds no data beyond this export.)*
