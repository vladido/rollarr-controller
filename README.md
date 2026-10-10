# Rollarr Controller

Fail-closed controller for rolling season packs using only Sonarr, Prowlarr (through Sonarr), and qBittorrent.

[Полная русская версия / Full Russian README](README.ru.md)

## Installation

Container image: `ghcr.io/vladido/rollarr-controller:latest` ([GHCR](https://github.com/vladido/rollarr-controller/pkgs/container/rollarr-controller)).

Pin an available `sha-<commit>` tag or digest for a fixed version; preserve the previous digest before updating.

Create a separate installation directory and save the following YAML as `compose.yaml` (complete example from [compose.example.yaml](compose.example.yaml)):

```yaml
services:
  rollarr-controller:
    image: ghcr.io/vladido/rollarr-controller:latest
    container_name: rollarr-controller
    restart: unless-stopped
    env_file:
      - ./config/rollarr-controller/controller.env
    volumes:
      - ./config/rollarr-controller/state:/state
    expose:
      - "8080"
    networks:
      - servarr

networks:
  servarr:
    external: true
    name: ${SERVARR_NETWORK:-servarr}
```

Requires Docker with Compose, running Sonarr and qBittorrent, Prowlarr indexers and a download client configured in Sonarr. The series and season must already exist and be monitored in Sonarr. Rollarr has no web UI of its own; use Servarr Tools UI for a dashboard.

1. Save the Compose example above; create the directories and protected environment file.
2. Create `config/rollarr-controller/controller.env` using the variables below, mode `0600`, and `ROLLARR_DRY_RUN=true`.
3. Create `config/rollarr-controller/state` and grant write access to UID/GID `1000:1000`, the container identity. Do not make it world-writable.
4. Join the existing external Docker network `servarr`, where Sonarr and qBittorrent are reachable. Set `SERVARR_NETWORK` if your shared network has another name; create the network beforehand.

```sh
mkdir -p config/rollarr-controller/state
umask 077
touch config/rollarr-controller/controller.env
chmod 600 config/rollarr-controller/controller.env
sudo chown 1000:1000 config/rollarr-controller/state
sudo chmod 700 config/rollarr-controller/state
```

Fill the environment file from the next section before starting. Sonarr/qBittorrent must join the same network; creating an empty network does not attach existing containers automatically. If needed, create it with `docker network create servarr`. Set `SERVARR_NETWORK` through the Compose `.env` or shell environment, not `controller.env`. Port `8080` is not published on the host.

### Required environment

```text
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=...
QBIT_URL=http://qbittorrent:8080
QBIT_API_KEY=...
ROLLARR_DRY_RUN=true
```

Optional: `ROLLARR_POLL_INTERVAL=900`, `ROLLARR_MIN_SEEDERS=1`,
`ROLLARR_MAX_BYTES_PER_EPISODE=12000000000`, `ROLLARR_CATEGORY=tv-sonarr`,
`ROLLARR_STATE_DIR=/state`, `ROLLARR_HEALTH_PORT=8080`, and `SERVARR_TOOLS_TOKEN`. When
`SERVARR_TOOLS_TOKEN` is unset, run-now control is disabled and `POST /v1/runs`
is unavailable. Keep the API port on a trusted, container-internal network.

Explicit automatic targets use
`ROLLARR_TARGETS=tvdb_id:season:tracker:topic,...` restricts release selection
for an existing monitored series to that exact topic. Rollarr never creates
series: absent or unmonitored series are skipped. Manage additions and monitoring
in Sonarr; the older `ROLLARR_ROOT_FOLDER` and `ROLLARR_QUALITY_PROFILE_ID`
settings do not authorize automatic creation.


### Start and safely enable live mode

Pull the published image and start the service:

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose logs --tail 100 rollarr-controller
```

Startup immediately schedules a cycle; the interval is at least 60 seconds. Review a completed cycle, rejected candidates, and audit proposals before enabling live mode. Set `ROLLARR_DRY_RUN=false` and recreate only Rollarr to enable live operation. Dashboard run-now uses the configured mode; it does not switch dry-run/live.

**qBittorrent authentication:** Rollarr uses the Bearer scheme in the `Authorization` header with `QBIT_API_KEY`; username/password login is not implemented. The standard qBittorrent Web API uses session login, so you need a compatible endpoint accepting Bearer before startup. Do not substitute a password for the API key.


## Contract

- Reads aired monitored Wanted episodes from Sonarr; explicitly watched topics
  also include monitored missing episodes whose upstream air date is absent.
- Runs series/season-bound Sonarr searches and downloads the real `.torrent` metadata.
- Verifies exact season/file inventory, canonical title or alias, season air year
  or the preceding production year, seeds, size, topic identity, monotonic
  endpoint, and unseen infohash. Older same-title collisions remain rejected.
- Rewrites a verified compound tracker title to Sonarr's canonical series title
  before submission, then requires Sonarr's parser to return the same series,
  season, and episode scope before downloading or grabbing the candidate.
- Treats Sonarr's zero-runtime size warning as provisional only; Rollarr still
  validates the downloaded metainfo inventory and byte totals before a grab.
- Ranks equally complete packs by Sonarr quality weight and Custom Format score
  before seed count or size. An adopted topic that does not cover the
  first Wanted episode does not block a verified expanded topic.
- Sends initial grabs and later topic updates through Sonarr. An expanded pack
  may therefore be downloaded in full; traffic reuse is not promised.
- Records a topic endpoint and hash in SQLite only after Sonarr reports every
  episode represented by that torrent as imported (`hasFile=true`).
- Persists submissions as pending and reconciles them after import even when
  the series disappears from Wanted. Older submissions are recovered only
  from matching audit and validated saved metainfo during migration.

Default mode is `ROLLARR_DRY_RUN=true`. A healthy container means only deployed; production readiness requires a real append-only update and native Sonarr import.

## Observability and control API

The stdlib HTTP server listens on `ROLLARR_HEALTH_PORT` (default `8080`). The
Compose example exposes it only to the Compose network, not to the host.

- `GET /live` checks process liveness independently of cycle outcome.
- `GET /health` preserves the legacy health response.
- `GET /v1/health` reports health plus service/version, configured dry-run,
  queue/running state, capabilities, and the latest run metadata.
- `GET /v1/status` reports the same runtime/configuration metadata without
  changing its HTTP status after a failed cycle.
- `GET /v1/audit?limit=N` returns newest-first, allowlisted audit records. `N`
  is clamped to `1..200`; malformed trailing JSONL is ignored. Torrent data,
  hashes, tracker topics, URLs, and credentials are not returned.
- `POST /v1/runs` queues one manual run and returns `202`, or `409` when one is
  already queued. It requires the server-side `SERVARR_TOOLS_TOKEN` in an `Authorization` header with the Bearer scheme and
  accepts an optional `Idempotency-Key` header. Replays of a key within the
  bounded five-minute cache return the original run and its current status
  (`queued`, `running`, `completed`, or `failed`) without starting another scan.
  Engine scans execute only in the single worker shared with scheduled runs.

A failed latest cycle makes both health endpoints return `503`, even if an
earlier cycle succeeded. Error details are reduced to the exception class.



## Troubleshooting, updates, and backup

- `/live` works but `/v1/health` returns `503`: the process is alive and its latest cycle failed. Inspect `last_run`, `last_error`, and Sonarr/Prowlarr search availability; an earlier success does not cancel a newer failure.
- Candidate rejected: check its audit reason, `.torrent` inventory, episode monitoring, quality, and seeds. Do not bypass identity checks to accept an ambiguous release.
- `waiting_for_import`: a grab was submitted; confirmation waits for every episode's `hasFile=true`. Inspect Sonarr queue/import rather than deleting the torrent to retry.
- Wanted is read as one page with `pageSize=1000`; larger inventories need a separate completeness assessment.

To update, preserve configuration and state, enable dry-run, run `docker compose pull` and `docker compose up -d`, then optionally download [scripts/check-after-update.sh](scripts/check-after-update.sh) and run it with `sh check-after-update.sh`. The script checks container `rollarr-controller` and a completed successful cycle, not media-import correctness. Roll back to a previously preserved image tag/digest without deleting state.

Back up the protected environment file and the complete state directory: `ledger.db`, `audit.jsonl`, and saved metainfo. Stop only Rollarr while copying SQLite for a consistent snapshot, or use the SQLite backup API. Losing the ledger loses accepted topics, seen hashes, and pending-import records. Keep this data out of Git.

## License

MIT — see [LICENSE](LICENSE).

## Developers: optional source build

With Git and Docker, build a local development image and set `image` in a local Compose copy to `rollarr-controller:dev`. The configuration, secrets and network requirements above also apply to development.

```sh
git clone https://github.com/vladido/rollarr-controller.git
cd rollarr-controller
docker build -t rollarr-controller:dev .
```
