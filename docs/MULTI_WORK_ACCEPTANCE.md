# Multi-work host acceptance

This runbook records the hardware acceptance evidence for multi-work
collections. Run it on the Raspberry Pi after `make check` and
`make acceptance` pass in a clean checkout. It is intentionally read-only
toward MP4 files. Sidecar corrections are the only permitted media-tree
writes.

Do not paste configuration files, environment dumps, OpenAI responses, API
keys, full personal paths, or media filenames into an acceptance report.
Record collection totals and sanitized mapping counters instead.

## Preparation

1. Record the commit with `git rev-parse --short HEAD`.
2. Record the Pi model, OS, architecture, Python, Nginx, and free disk space.
3. Back up the installed configuration and state:

   ```sh
   make backup BACKUP_DIR=/var/backups/rpi-streamer
   ```

4. Validate the checkout and installed configuration:

   ```sh
   make check
   make acceptance
   make validate
   docker compose config --quiet
   ```

5. Record database and site sizes without recording their absolute paths:

   ```sh
   sudo du -sk /var/lib/rpi-streamer/catalogue.db /var/lib/rpi-streamer/site
   ```

## Upgrade and cold scan

Deploy using the repository-root update path:

```sh
make update MEDIA_ROOT=/mnt/media
sudo systemctl status rpi-streamer --no-pager
```

If the installed configuration already has the correct `media_root`,
`make update` preserves it; the explicit value above controls newly rendered
deployment defaults. Confirm the pre-existing simple-title collection retains
its title URL and media URL.

To measure a cold multi-work scan without deleting provider metadata, use the
operator command to invalidate only the selected collections' model mappings
and exact model caches, then stop the service and run one foreground scan under
`time`:

```sh
sudo systemctl stop rpi-streamer
sudo /opt/rpi-streamer/venv/bin/rpi-streamer \
  --config /etc/rpi-streamer/rpi-streamer.ini \
  mapping invalidate-model 'MF Ghost'
sudo /opt/rpi-streamer/venv/bin/rpi-streamer \
  --config /etc/rpi-streamer/rpi-streamer.ini \
  mapping invalidate-model 'Tsuki ga Michibiku Isekai Douchuu'
sudo /usr/bin/time -v /opt/rpi-streamer/venv/bin/rpi-streamer \
  --config /etc/rpi-streamer/rpi-streamer.ini scan
sudo systemctl start rpi-streamer
```

Record elapsed time and maximum resident set size from `time`. Inspect mapping
state with the bounded, redacted command:

```sh
sudo /opt/rpi-streamer/venv/bin/rpi-streamer \
  --config /etc/rpi-streamer/rpi-streamer.ini \
  mapping inspect 'MF Ghost'
sudo /opt/rpi-streamer/venv/bin/rpi-streamer \
  --config /etc/rpi-streamer/rpi-streamer.ini \
  mapping inspect 'Tsuki ga Michibiku Isekai Douchuu'
```

Count manual, deterministic, model, ambiguous, and unmapped results. Review
the generated page and record incorrect mappings as a number, without listing
personal filenames.

## Manual correction and cached rescan

Add one harmless exact override or work-range correction to a collection
sidecar, validate it, and rescan:

```sh
sudo systemctl stop rpi-streamer
sudo /opt/rpi-streamer/venv/bin/rpi-streamer \
  --config /etc/rpi-streamer/rpi-streamer.ini \
  mapping validate-sidecar 'MF Ghost'
sudo systemctl start rpi-streamer
sudo systemctl reload rpi-streamer
```

Confirm the correction wins on the next scan. Restore the original sidecar
afterward unless the correction is genuinely desired.

Send another `SIGHUP` without changing files or rules. Compare these sanitized
events before and after:

```sh
journalctl -u rpi-streamer --since '15 minutes ago' \
  --grep 'event=(mapping_stats|external_calls|scan_issues|scan_finished)' \
  --no-pager
```

The cached pass must not add provider/model failures, and valid cached model
results must report cache hits rather than new mappings. The
`event=external_calls` line records actual provider HTTP attempts (including
retries) and OpenAI requests for that scan without logging request content.
Record elapsed time, peak RSS from
`systemctl show rpi-streamer -p MemoryPeak`, database/site growth, and aggregate
mapping counts.

## Browser and Nginx

Verify:

- MF Ghost has three groups with work-local numbering.
- Tsukimichi has two groups and provider context for both works.
- Previous/Next crosses group boundaries with one video player.
- seeking works in both directions;
- the no-JavaScript list contains every mapped and unmapped file;
- a simple-title page and its previous URL still work.

Use a non-sensitive media URL copied from the local page for a range check:

```sh
curl -sS -D /tmp/rpi-streamer-range.headers \
  -H 'Range: bytes=100-199' \
  -o /tmp/rpi-streamer-range.body \
  'http://127.0.0.1/media/REDACTED.mp4'
wc -c /tmp/rpi-streamer-range.body
grep -E 'HTTP/|Content-Range|Accept-Ranges' \
  /tmp/rpi-streamer-range.headers
```

Expect HTTP 206 and exactly 100 response-body bytes.

## Offline independence

Back up the INI first. Test these configurations one at a time, restoring the
normal configuration after each:

1. `openai_fallback_enabled = false` with `metadata_provider = tenrai`;
2. `metadata_provider = none` with the existing OpenAI setting;
3. both disabled.

After each edit, run `validate-config`, restart, rescan, and confirm manual
mappings, cached metadata, generated pages, and MP4 playback remain available.
An uncached provider work may remain pending; it must not hide media or replace
the last published site.

## Sanitized evidence record

Copy this table into release notes and fill it with aggregate values:

| Field | Result |
|---|---|
| Commit | Pending |
| Pi / OS / architecture | Pending |
| Python / Nginx | Pending |
| Collection titles / files | Pending |
| Cold elapsed / peak RSS | Pending |
| Cached elapsed / peak RSS | Pending |
| Provider calls cold / cached | Pending |
| OpenAI calls cold / cached | Pending |
| Database growth / site growth | Pending |
| Manual / deterministic / model mappings | Pending |
| Ambiguous / unmapped / incorrect | Pending |
| MF Ghost 3 groups | Pending |
| Tsukimichi 2 groups | Pending |
| Manual correction wins | Pending |
| Playback / seeking / no-JS / HTTP 206 | Pending |
| OpenAI-off / Tenrai-off / both-off | Pending |

Release acceptance is complete only after every row is recorded and no item
is outstanding.
