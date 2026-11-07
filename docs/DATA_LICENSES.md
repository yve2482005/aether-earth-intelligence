# Data Licenses

Seeded 2026-10-05 from each provider's published terms (checked via web
search on that date). "Seeded" means: recorded here and in the
`source_registry` table as the starting point. Re-verify against the live
terms page before any production use or redistribution, because terms change.

## Open-Meteo (weather, air quality)

- **License (API data):** CC BY 4.0 (Attribution 4.0 International). The
  geocoding endpoint's docs state CC BY-NC 4.0 — the non-commercial variant
  applies to geocoding specifically.
- **Attribution required:** yes. Display "Weather data by Open-Meteo.com"
  with a link to https://open-meteo.com/ next to any location where the data
  is shown.
- **Commercial use:** free for non-commercial use; contact Open-Meteo for
  commercial use. Self-hosting their AGPLv3 code is an alternative.
- **Redistribution:** allowed under CC BY 4.0 with attribution; commercial
  redistribution falls under the commercial-use rule above.
- **Rate limits / fair use:** ~10,000 requests/day; contact them if exceeded.
  Data provided as-is, no warranty, no uptime SLA on the free tier.
- **Terms page:** https://open-meteo.com/en/terms (verify before production)

## USGS earthquake feeds (earthquake)

- **License:** US federal government work — public domain. (USGS generally
  releases its data products into the public domain; verify the specific
  feed's terms page before production.)
- **Attribution:** credit USGS as the source; standard practice, e.g.
  "Earthquake data: U.S. Geological Survey".
- **Commercial use:** public-domain data may be used commercially.
- **Redistribution:** allowed (public domain); do not imply USGS endorsement.
- **Status:** terms not yet re-verified against the live USGS page at seed
  time — marked `verify_before_production` in the registry seed.

## NWS / api.weather.gov (alerts, tsunami CAP, US observations)

- **License:** US federal government work — public domain.
- **Attribution:** credit the National Weather Service; do not imply NWS
  endorsement and do not present modified content as official.
- **Commercial use:** allowed (public domain).
- **Redistribution:** allowed with the no-endorsement / not-presented-as-
  official conditions above.
- **Technical requirement:** an identifying `User-Agent` header is mandatory
  (empty UA returns 403). Rate limit unpublished; described as generous for
  typical use.
- **Tsunami note:** redistribution of official warning messages must keep
  the original classification wording intact (see TSUNAMI_SYSTEM.md).

## Tsunami warning centers (PTWC and similar official issuers)

- **Status:** official-use messages. Redistribution terms vary by issuer and
  were **not verified** at seed time. Treat as `verify_before_production`:
  relay with attribution, keep original classification, and confirm each
  issuer's terms before any redistribution beyond the app.
- **Rule (hard):** only official messages are ingested, whatever the license.

## Google Sheets / Drive (our mirrors, not data sources)

- Governed by the Google Workspace / Drive API terms under the operator's
  own Google account. No user data is sent there unless the operator
  configures credentials; the mirrors carry only summary/control data.

## How this is enforced in code

- `source_registry.license`, `.attribution`, `.commercial_use`,
  `.redistribution` columns exist for every source.
- `scripts/seed_registry.py` (backend team) seeds these values; keep this doc
  and the seed values in sync.
- The UI shows attribution strings from the registry, not hard-coded text.

## Honest limitations

- Two entries (USGS detail terms, tsunami-center redistribution) are seeded
  as **unverified** and marked accordingly. Do not treat this doc as legal
  advice; the operator must re-check the live terms pages.
- License terms change. This file's "seeded 2026-10-05" stamp is the
  freshness signal — if it is old, re-verify.
