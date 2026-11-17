# Thermal history backup snapshots

This changes diagnostic-history persistence only. It does not alter room settings,
safety thresholds, controller decisions, equipment commands, or normal live
recording. It requires no Core or Supervisor changes. It prevents Home Climate's
writer from remaining frozen when another integration fails backup preparation;
it cannot repair that other integration's failure or guarantee overall backup
success.

## Authority and lifecycle

- Working SQLite database and WAL: `.cache/house_climate_thermal.sqlite` and
  sidecars, excluded from stock HA configuration archives.
- Backup authority: `.storage/house_climate_thermal.snapshot`, a closed immutable
  envelope containing a compact compressed SQLite database.
- Initial setup/migration creates and verifies a snapshot before switching away
  from the legacy database. Normal restart validates and retains the working DB.
- Each stock pre-backup hook refreshes the snapshot through SQLite online backup
  on a separate read connection, while the writer continues to commit. There is
  no periodic full-database rewrite, writer pause, persistent backup latch, or
  post-hook dependency. The post hook is deliberately a no-op.
- Snapshot publication is serialized across runtime reloads. A cancelled caller
  can leave its bounded executor work running until completion, but cannot leave
  a writer fence behind. Shutdown requests cancellation through a thread event.
- Snapshot work has a 30-second cooperative deadline (SQLite progress callbacks,
  streaming checks); OS filesystem calls such as fsync can exceed it. Lock/startup
  waits are separately bounded at 60 seconds. Failure is visible as a failed
  pre-hook, a warning and snapshot diagnostics; prior working history remains.
- For an already migrated valid working DB, failure to refresh on ordinary
  restart does not stop recording. First migration/restore failures do not fall
  through to creating empty history.

## Why the fixed envelope is necessary

Stock securetar ultimately calls `tarfile.add`, which stats a path before opening
it. Atomic rename alone is insufficient for variable-size files: replacing a
smaller file with a larger database at this boundary silently truncates the
archive; replacing a larger file with a smaller one can fail the archive.

Every published envelope inode has exactly **1,074,790,400 bytes** (1 GiB + 1 MiB).
The header contains version, codec, payload lengths, generation, capture time,
last decision timestamp, and SHA-256 digests. Publication writes a new excluded
staging file, fsyncs, then atomically renames it. Readers get a complete old or
complete new inode with the same length. No published inode is modified in place.
SQLite integrity, schema and required tables are checked. Restore validates
lengths, decompression bounds, both digests and SQLite integrity before installing
its working database without overwriting an existing file/sidecar.

The uncompressed compact database is bounded at the existing **1 GiB** history
budget, not a smaller silently imposed retention limit. Compression overflow or
budget violation rejects refresh; it never drops retained records to fit. The
existing raw/rollup/episode retention policy is unchanged. Capacity must never be
changed in place; a future format needs a different pathname/version and explicit
migration rules.

## Disk and performance tradeoff

Zero padding is sparse on initial publication and compresses well in an archive.
**Stock restore does not preserve that sparseness.** A synthetic small-history
benchmark with actual securetar and production envelope size measured about
2.7 seconds to gzip the member (about 1.0 MiB compressed) and 4.7 seconds to extract
it, allocating the full 1.001 GiB. This is a cloud test machine, not a prediction
for the user's HA hardware. The live history database size was not available.

Supervisor restores through an extracted temporary tree and a copied config tree,
so it can temporarily materialize two envelope copies (over 2 GiB), in addition to
other HA files and subsequent working-DB extraction. Publication checks room for
two physical SQLite copies plus the existing 256 MiB reserve; restored DB import
checks its declared raw size plus that reserve. These checks cannot reserve space
against another process and cannot prevent stock Supervisor running out of space
before this integration starts. No-space errors preserve prior artifacts and do
not silently initialize empty history. A failed directory fsync after atomic
replace can mean a new valid snapshot exists but durability is unconfirmed; it is
reported as a failure, not successful publication.

## Restore and migration rules

Verified against stock Core 2026.11.1 and Supervisor 2026.11.0:

- Both configuration archive filters exclude direct `.cache/*` children and keep
  the `.storage` snapshot. Staging directories under cache are excluded as well.
- Supervisor full Home Assistant restore clears the configuration tree before
  copying archive data. With `exclude_database`, it preserves only the recorder
  names `home-assistant_v?.db` / `-wal`, not Home Climate's cache. Legacy v1 archive
  layout follows the same clearing path. A partial restore that does not include
  Home Assistant configuration naturally does not restore this history either.
- Core's own restore similarly clears configuration except explicitly retained
  backup/recorder files. Therefore a true supported configuration restore has no
  working cache: the validated archived snapshot is imported. A newer pre-restore
  cache cannot override it.
- Ordinary restart retains the validated working cache, including records newer
  than the last archived snapshot. There is no routine shutdown data loss.
- Manual file overlays that retain `.cache`, selective third-party restores,
  copying an envelope alone, and cache-cleaner deletion are **not** a supported
  restore protocol. There is no claim to detect intentional restoration of an
  identical old snapshot beside a newer surviving working DB. Explicit recovery
  must preserve/move the working DB and sidecars first; never manipulate them
  while HA is writing.
- Upgrade/migration requires the old history writer to have stopped cleanly. Do
  not proceed with in-place migration after a reported `shutdown_timeout`; resolve
  that failed unload first (a clean Core stop is the safe operational boundary).
- Legacy `.storage/house_climate_thermal.sqlite` is captured with SQLite online
  backup, including committed WAL. Only after validated snapshot and working copy
  exist are its closed files moved into excluded cache rollback evidence named
  `house_climate_thermal.legacy*`. Failures preserve originals. A crash between
  these moves still leaves a validated snapshot/working copy as authority.
- Downgrading to an older Home Climate version requires an explicit offline
  migration back to the legacy path. Simply installing an older version would
  not discover the new cache/envelope. Rollback files are historical evidence,
  not automatically a current database. The migration rollback copy is retained
  indefinitely in excluded cache, not counted as active history retention; its
  extra disk use requires explicit offline cleanup after rollback is no longer
  needed. Never replace newer history with them
  without reviewing the intended recovery point.

Snapshot recovery point is the online capture associated with a successful pre
hook. It is not a promise of a strict five-minute RPO. Diagnostics expose capture
metadata, age, progress and errors. Repeated failed refreshes can leave an older
last-good snapshot, but the failed pre-hook stops that backup attempt rather than
quietly claiming freshness. Loss is limited to diagnostic evidence newer than
whichever backup is actually restored; controls/settings remain separate.

## Reproduction and tests

Use the repository's Python 3.14 HA environment:

```sh
python -m unittest tests.test_thermal_snapshot -v
python -m unittest tests_homeassistant.test_thermal_storage tests_homeassistant.test_backup_manager_lifecycle -v
python -m unittest discover -s tests -q
python -m unittest discover -s tests_homeassistant -q
python scripts/package_hacs.py
```

Tests include actual stock HA failed-peer pre-hook aggregation without a post,
continued writes and retry, cancelled refresh, reload, WAL migration, failed
migration/refresh, corrupt restore, ordinary restart, real securetar stat/open
replacement and stock archive exclusion/restore clearing with a newer cache.
No tests connect to HA hardware or change the installed system.

Source references:
- [Supervisor archive/exclusions/restore, 2026.11.0](https://github.com/home-assistant/supervisor/blob/2026.11.0/supervisor/homeassistant/module.py)
- [Core archive exclusions, 2026.11.1](https://github.com/home-assistant/core/blob/2026.11.1/homeassistant/components/backup/const.py)
- [Core restore, 2026.11.1](https://github.com/home-assistant/core/blob/2026.11.1/homeassistant/backup_restore.py)
