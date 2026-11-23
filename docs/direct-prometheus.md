# Direct Home Climate Prometheus metrics

The optional Home Climate scrape endpoint is `GET /api/house_climate/metrics`
on the existing Home Assistant HTTPS origin. It is **off by default**. It needs
no new daemon or Home Assistant user, and grants no access to HA's API or
equipment. This is a bounded read of current in-memory runtime data and the
already-loaded mode journal. The scrape never opens SQLite or reads HA
Recorder. Existing Home Climate entities, the built-in HA Prometheus
integration, and stored thermal history remain unchanged.

## Enable, rotate, or disable

Create a **unique** 32-byte random secret (64 lowercase hex characters) for this
scrape job on the Prometheus host. Store it in a protected file with no trailing
newline. The following example prints only the SHA-256 digest, not the secret:

```sh
umask 077
openssl rand -hex 32 | tr -d '\n' > home-climate-metrics.token
openssl dgst -sha256 -r home-climate-metrics.token
```

On Home Assistant, create `home_climate_metrics.json` in the HA configuration
directory with the first field of that digest output:

```json
{"token_sha256":"REPLACE_WITH_THE_64_CHARACTER_LOWERCASE_HEX_DIGEST"}
```

Keep even the digest out of Git and shared diagnostics. The secret must not
appear in URLs, shell history, or proxy/header logs. Reload Home Climate to
apply the file (a normal integration reload may briefly interrupt comfort
control). When disabled, stopping, or not loaded, the route cannot return
metrics. The route is mounted only once per HA process and resolves the current
integration instance on each request.

The credential is **only** valid in the `X-Home-Climate-Metrics-Token` header
on this route. A Home Assistant bearer token does not authenticate it, and the
scrape token does not confer any Home Assistant privileges. The view uses HA's
existing HTTP server and does not change the authentication or ban settings of
other routes. Use HTTPS with certificate verification. If you already
terminate TLS at a reverse proxy, ensure HA recognises the HTTPS scheme via its
existing trusted-proxy configuration; never broaden proxy trust or turn off
certificate verification for scraping.

To rotate, replace the secret and its digest, reload Home Climate, and verify
the old secret now receives `401` while the new one returns `200`. To disable,
remove the JSON file, reload Home Climate and remove its scrape job. The
route then returns `404`; no change to the old HA scrape is required. Malformed
credential settings fail closed without disabling comfort control.

## Parallel scrape configuration

Add a separate scrape job, keeping the existing Home Assistant scrape:

```yaml
scrape_configs:
  - job_name: home_climate_direct
    scheme: https
    metrics_path: /api/house_climate/metrics
    follow_redirects: false
    scrape_interval: 30s
    scrape_timeout: 5s
    sample_limit: 450
    http_headers:
      X-Home-Climate-Metrics-Token:
        files: [/run/secrets/home-climate-metrics.token]
    static_configs:
      - targets: [ha.example.net:443]
```

Use a Prometheus version that supports [`http_headers.files`][config].
Keep redirects disabled so a misconfigured reverse proxy cannot send the
secret to another origin. Validate with `promtool check config` before
reloading the scrape configuration.

## Metric contract

The endpoint exposes Prometheus [text format 0.0.4][format]. Metric names
start with `house_climate_`. All values are gauges; absent evidence is omitted.
No entity IDs, event/trace IDs, raw temperatures, human-readable reasons,
configuration hashes, or credentials are exported. Labels are limited to
stable logical configured `head` and `room` keys (1–48 lower-case ASCII
letters/digits/underscores; at most 16 heads and 32 rooms), and the fixed
`mode` values `heat` and `cool`. Out-of-scope labels are omitted.

| Metric suffix | Contract |
| --- | --- |
| `worker_snapshot_fresh` | 1 when the last live/preview worker snapshot is at most the larger of 30 seconds or three polling intervals old; 0 otherwise. |
| `worker_snapshot_age_seconds` | Wall-clock age of the most recent live/preview worker snapshot, including when stale. Omitted without a usable timestamp. |
| `decision_age_seconds` | Estimated age of the last full comfort decision: snapshot age plus the elapsed part of the reported decision interval. In maintenance this is not reset by the scrape or by a new maintenance report. |
| `worker_live` | 1 live or 0 Preview in a fresh snapshot. Not a guarantee that an individual command was allowed. |
| `selected_mode_code` | The fresh plan's shared direction; 1 Off, 2 Heat, 3 Cool. Not a dispatched command. |
| `room_demand{room,mode}` | 1 or 0 when the fresh plan has an explicit room demand flag. Not measured heat or compressor activity. |
| `head_blocker_code{head,mode}` | 0 no structured blocker was recorded (not permission to start); 1 missing/invalid room evidence; 2 house limit; 3 opposite comfort limit; 4 comfort rearm; 5 unavailable mode; 6 other head inhibition in the selected direction. Structured constraint takes precedence over unavailable mode, then generic inhibition. |

Head journal families are `head_command_*`, `head_handoff_*`,
`head_feedback_*`, and `head_handoff_feedback_*`. Their suffixes, where present,
are `mode_code`, `path_code`, `reason_code`, `outcome_code`,
`attribution_code`, `dispatched_timestamp_seconds`,
`finished_timestamp_seconds`, `timestamp_seconds`, and
`handler_duration_seconds`. Commands include mode/path/reason/outcome, dispatch
and finish; handoff omits path/reason because they are fixed to the handoff.
Feedback and handoff feedback only have mode, report timestamp and attribution.
Handler duration is finish minus dispatch for the same latest recorded command.
The existing [mode command documentation][journal] defines every journal code.
Those gauges are historical and may persist across a restart; their timestamps,
not scrape time, indicate when the event was recorded. Handler return or
matching HA context is not proof of physical device operation. There are no
transition counters yet: a scraper cannot reconstruct events between scrapes
from latest-value gauges.

## Migration and rollback

1. Keep both scrape jobs in parallel, and confirm
   `up{job="home_climate_direct"} == 1` and initially expected series limits.
2. Compare several naturally occurring normal and handoff observations against
   the existing HA numeric diagnostic gauges. Compare timestamps, code
   meanings, feedback attribution and stale/missing behavior. Do not create
   an equipment transition just to populate a gauge.
3. Update dashboards or alerts only for metrics that match their intended
   semantics. Keep existing HA source/room/electrical metrics where the new
   endpoint deliberately does not provide equivalent families. Verify the new
   stale-worker signal independently before migrating active alerts.
4. Roll back by removing the new scrape job and optional digest file, then
   reloading Home Climate. No entity, existing scrape, or history migration
   is required.

[config]: https://prometheus.io/docs/prometheus/latest/configuration/configuration/#http_config
[format]: https://prometheus.io/docs/instrumenting/exposition_formats/
[journal]: mode-command-diagnostics.md
