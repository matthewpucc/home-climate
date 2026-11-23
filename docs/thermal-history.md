# Durable thermal evidence (stage 2 foundation)

Home Climate records its existing observations in an integration-owned SQLite
file, `.storage/house_climate_thermal.sqlite`, **not** in HA Recorder. This stage
does not issue fan commands, authorize experiments, or implement a learned model.
The existing 22:00–09:00 experiment restriction remains unchanged. Passive
recording can continue overnight. Only one Home Climate runtime is currently
supported by the integration.

## What is recorded

- Allowlisted controller decision snapshots: room/source evidence, active comfort
  targets, planned mode and head targets, cached fan evidence, windows, outdoor
  evidence, purifier levels, inhibitions and warnings.
- Distinct qualified source reports with original source timing and timestamp
  kind. Acquisition/packet timestamps take precedence over HA report time.
  Repeated readings of the same report do not add samples. Identical maintenance
  snapshots are coalesced to at most one per 90 seconds; changed evidence or
  context is retained immediately. A source's HA report
  is not necessarily an independent physical measurement.
- **Planned-demand episodes**, split on room demand, relevant target/fan/context
  changes, missing room readings, restart, a decision gap over 180 seconds, or
  configuration changes. These are not confirmed compressor runs, physical fan
  speeds, or verified command acknowledgments. Episodes retain their context,
  observed fused extrema and number of decisions. Their end is the last observed
  decision, not an inferred completion time.

Individual report capture currently requires source freshness `qualified` and a
strongest timestamp no older than 180 seconds at the decision. Rejected/stale
source evidence remains in the detailed decision, not converted into numeric
zeroes. Uninstrumented internal heat loads and unknown source acquisition times
remain unknown. No pre-install data is backfilled from Recorder or Prometheus.

Configuration hashes and source mappings preserve identity across changes.
**Physical relocation is not automatically detected**: change the source/config
identity before mixing placement regimes. An explicit placement-version editor,
independent physical command evidence, outcome/coast metrics, time-weighted
coverage and persistent learned models remain later work. Current summaries must
not be used as proof that a live trial is eligible.

## Retention and honest lookback

Initial constants in `Retention` (not yet a UI setting):

| Record | Retention |
|---|---|
| Detailed decisions and distinct source observations | 30 days |
| Five-minute source summaries | 365 days |
| Hourly source summaries | 1,095 days |
| Closed planned-demand episode summaries/context | 365 days |

Cleanup converts old, closed source buckets to summaries and removes detail in
**one SQLite transaction**. A failure rolls back both operations. The summaries
retain count, sum, sum of squares, extrema, first/last report time and longest
between-report gap; hourly aggregation combines these sufficient statistics,
not averages of averages. They are **sample/report-weighted**, not time-weighted.
No interpolation or full-interval coverage is claimed. Edge gaps and missing
buckets remain missing. Different sources and configuration regimes never merge.
Raw decisions are removed only after the corresponding old source backlog has
been summarized. Full contextual snapshots are not reconstructible afterward;
compact episode context remains for the episode retention period.

Maintenance processes at most 100 raw and 100 five-minute source buckets and
1,000 decision/episode rows per pass, at most once a minute during incoming
recording. It reuses freed pages and checkpoints WAL; it does not run a full
VACUUM. A large initial backlog may take multiple passes. Hourly summaries and
closed episodes expire at their retention boundary. Open episodes close as
interrupted on the next session, not as successful completed trials.

## Query and diagnostics

Developer Tools → Actions: `house_climate.thermal_history_query` accepts UTC Unix
`start` and exclusive `end` seconds, plus `limit` (1–1,000, default 200). It returns
detailed decisions, source observations, overlapping summaries and episodes,
with explicit truncation flags and availability labels. Decision rows are
additionally capped at 32, and the total serialized row budget is 2 MiB. Narrow
the time window if truncated. A query uses a consistent read-only transaction,
a one-second lock timeout and a five-second query budget. It never creates a
missing DB or operates equipment. Summary buckets are returned in full when
they overlap the requested range, not prorated into a misleading partial mean.

Integration diagnostics include storage status, last commit, queue depth, dropped
and committed record counts. Counters currently cover the current process;
detailed decisions include the process's cumulative dropped-record count. A
missing segment is not proof of stable conditions. Raw lookback and summary
availability are separate; `coverage_complete` is deliberately false.

### Optional read-only MCP tool

Home Climate can publish the dedicated `house_climate_thermal_history` tool set
with one tool, `GetThermalHistory`. It is **off by default**. In Home Climate's
integration options, enable **Allow read-only thermal history through MCP** only
if you intend authenticated Home Assistant administrators to be able to query
this history from an MCP client. HA's [MCP Server integration][ha-mcp] must
already be configured. An already-connected client may need to reconnect or
refresh discovery. This Home Climate option does not enable live
equipment commands, install/configure MCP Server, or change any existing
connector or credentials.

For just this tool set, point the client at **`/api/mcp/house_climate_thermal_history`**
on your Home Assistant server and use an authorized Home Assistant administrator
connection. Use HA's main MCP URL only if you intend to share its other
configured tool sets; an administrator's dedicated API URL does not inherit
the main URL's filter or read-only setting. This tool set contains no writes
and is not offered to conversation/voice clients. Turning the Home Climate
option off revokes this tool set independently of the MCP Server settings.
Treat the history as private: it can reveal room labels, temperatures and
activity patterns. Do not expose a general administrator token to an
untrusted client.

Give `start` (**inclusive**) and `end` (**exclusive**) as past UTC Unix
seconds, with a maximum span of **7 days**, and optionally `limit` (**1–50**
per category; default **20**). At most **32 decision** rows, **64 KiB** of
projected result data and **one in-flight query** are allowed. The query only
reads the Home Climate database and the current integration entry; it cannot
select a file, another entry, an arbitrary entity or SQL. A reload or access
revocation invalidates outstanding results. There is no room parameter, so
every result may contain multiple rooms.

The response includes projected decision temperatures and request targets,
source observations, rollups and planned-demand episode summaries, but not
raw decision or episode context, free-form warnings, internal
entry/session/source IDs, raw configuration metadata, or other HA history.
Read the [enum reference](thermal-history-enums.md) for exact values and
attribution limits. A successful empty result differs from an unavailable or
busy query. Overlapping rollup buckets are not split or interpolated, retained
data is not guaranteed to cover the interval, and reported HVAC commands are
not measured physical operation.

[ha-mcp]: https://www.home-assistant.io/integrations/mcp_server/

## Resource and failure behavior

One daemon writer handles SQLite off HA's event loop with WAL, FULL synchronous
commits, a one-second busy timeout and a bounded 128-record queue. Producers do
not await the DB. A full queue drops a record and increments diagnostics. Setup
failure is visible and requires reload after repair; subsequent submissions are
rejected and counted rather than silently queued to a dead writer.

Initial safeguards pause ingestion below 256 MiB filesystem free space or when
used database pages plus WAL reach 1 GiB. These are conservative initial budgets,
not measured growth estimates or hard physical-file ceilings: allocated empty
pages are reusable, and a transaction can grow the file beyond the preflight
budget. Maintenance remains possible under pressure. Failed cleanup/ingestion
records an error; ordinary comfort control, manual commands and protective Off
never depend on storage success. No trial or learner is enabled by this health
flag. Avoid repeated warning floods while diagnosing prolonged storage errors.

On shutdown, queued data may be discarded and counted; SQLite work is interrupted
and the thread join is bounded to two seconds **after equipment shutdown work**.
Restart opens a new recording session, never interpolating across downtime.
Schema versions are checked before modifying tables. Unknown future versions are
preserved and rejected, not reset. This is the first schema; future migrations
must include transaction, backup and recovery tests.

## Backup and restore

The database lives in the Home Assistant configuration directory at
`.storage/house_climate_thermal.sqlite`. Configuration backups include this path;
the Recorder-history exclusion concerns Recorder's own database, not this file.
A backup that omits the Home Assistant configuration cannot preserve this history.

The integration implements Home Assistant's official backup pre/post hooks.
Before the configuration is archived, the evidence writer finishes its current
transaction, checkpoints WAL with TRUNCATE and closes its connection. Preparation
has a five-second wait bound and fails explicitly if a consistent checkpoint is
unavailable. Generation-specific acknowledgements prevent a cancelled or previous
backup from approving a new one. A shared fence also blocks replacement writers
if the integration reloads mid-backup. Cancellation releases the pause. After
backup, the writer reopens
and resumes. Ordinary climate control and safety commands never wait for this
writer; observations queue during backup and overflow is counted as dropped data.
Pending, uncommitted queue records are not claimed as included in the backup.

Restore tests copy the standalone checkpointed database after a committed write
that previously existed in WAL, reopen it, and verify the evidence survives.
This verifies SQLite restore consistency, not a completed live Supervisor restore.
Use HA's normal backup lifecycle, not a manual copy of an actively written main
file. No generic DB-file export or automatic full VACUUM is implemented here.
