# Rolloff Ready

See whether a roll-off container and its delivery truck have room at a US home before requesting a quote.

Search an address, position connected container/truck footprints on satellite imagery, answer a short site questionnaire, and send a placement summary for a quote or delivery review. The tool surfaces the operational questions that a container's dimensions alone miss: unloading space, sideways or uphill slopes, grade transitions, overhead obstructions, and street access.

## Run locally

Requires Node.js 22 and Python 3.11+.

```sh
npm ci
```

Copy `.env.example` to `.planning/.env` and enter `HERE_MAPS_API_KEY`. If your credential file is already there, preserve it. `HERE_MAPS_APP_ID` is optional and is not used for API-key authentication. Never put the key in a `VITE_` variable.

In one terminal:

```sh
python api/dev.py --env .planning/.env
```

In another:

```sh
npm run dev
```

Open the Vite address, normally `http://127.0.0.1:5173`. The frontend proxies `/api` to `127.0.0.1:7071`. Maps and screening use the Python standard library locally; no Azure emulator is required. To test real email locally, install `api/requirements.txt` in a virtual environment and configure `ACS_CONNECTION_STRING` and `EMAIL_SENDER`. The only recipient is `jjfeore@gmail.com`.

For a production-bundle preview:

```sh
npm run build
python api/dev.py --env .planning/.env --static dist
```

Open `http://127.0.0.1:7071`. The development server binds only to loopback and is not a public production server.

## Implementation

- React, TypeScript, Vite and Leaflet render a responsive guided workflow. Geographic calculations preserve footprint dimensions and the joined container/truck edge when moved or rotated. Pointer controls have keyboard and slider alternatives.
- Python handles HERE imagery/address requests, USGS elevation sampling, validation, screening and contact submission. Azure Static Web Apps Free hosts the frontend and managed Python 3.11 Functions; there is no database or separate Functions plan.
- HERE geocoding is constrained to the US. The server signs returned address selections and verifies them again before assessment or contact submission. Satellite tiles are proxied as JPEG; credentials never enter browser URLs. Provider attribution is refreshed daily.
- The questionnaire drives a conservative, explainable result. Unknowns and delivery complications request operator review. A favorable screen still requires operator confirmation.
- USGS 1-meter terrain coverage is checked after address selection. After placement, four nearest-neighbor elevation samples estimate lengthwise and side-to-side tilt. Subtle question highlights and source notes are advisory; no answer is preselected. Signed evidence binds the estimate to its address, placement, rotation and container size for inclusion in the email.
- Azure Communication Services Email sends the full placement and questionnaire summary to a fixed recipient. The response distinguishes provider acceptance from mailbox delivery. Failed or unconfigured email never produces a success message; the customer can download the summary.

The canonical representative dimensions are in `api/rr/catalog.json`, shared with the frontend. Containers vary by supplier. The model adds 20% to a published 36-foot roll-off truck, giving a 43.2-foot straight truck envelope with the selected container's width. It does not simulate swept turning paths, stabilizers, ground strength, curb transitions, overhead clearance, or complete delivery maneuvers.

### USGS terrain estimates

The app uses the free public [USGS 3DEP elevation service](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer), with no additional API key or cloud resource. High-resolution coverage varies by address. A catalog match alone is insufficient: the adapter checks native raster resolution and valid samples, and uses a single source for all four points. NoData, coarse or inconsistent sources, and provider failures are explicit unavailable states rather than zero slope.

Lengthwise sampling runs from the cab end of the truck clearance to the container's far end. Lateral points are 1.5 meters beyond either edge at the midpoint of the combined footprint. Percent slope is elevation difference divided by the geodesic baseline, multiplied by 100. Positive lengthwise slope means uphill from truck to container. Positive lateral slope means the right side is higher when looking toward the container.

An absolute 3% slope changes the visual note from low estimated tilt to a review cue. This is an adjustable product cue, not a manufacturer-approved unloading limit. Four terrain samples cannot resolve a curb, road crown, grade break, bridge deck or separate planes beneath the truck and box. Source resolution is not surveyed accuracy. All manual questions remain required, and their answers determine the screening result. Moving or rotating the footprint or changing size clears old estimates. No HERE Map Attributes calls are made.

## Verification

```sh
npm run check
npm test
npm run build
python -m unittest discover -s api/tests -v
python -m unittest discover -s scripts/tests -v
```

Tests cover geographic dimensions and shared-edge geometry, four-point slope calculations, coverage gaps, provider failures, stale UI responses, signed terrain evidence, screening outcomes, signed US address selection, invalid/malicious input, secret redaction, duplicate submissions, and deployment/teardown safeguards. CI also compiles both Bicep templates.

## Deploy and remove

See [the infrastructure guide](infra/README.md). The private handoff includes detailed steps in `.planning/USER-SETUP.md`; that directory is deliberately not committed. After one-time Azure sign-in and OIDC bootstrap, **Provision Azure** creates project resources and **Deploy Azure** publishes the app. Subsequent main pushes validate and deploy when enabled. **Teardown Azure** removes only the dedicated, ownership-tagged project resource group after typed confirmation.

SWA is explicitly on its Free plan. HERE usage follows the account plan; ACS Email has usage-based charges. No cloud services are silently provisioned by `npm` or local development. Deployment stays disabled until bootstrap succeeds.

## Security and data handling

Secrets stay in ignored environment files, GitHub environment secrets and Azure application settings. The API uses fixed provider endpoints, bounded input and response sizes, HTTPS verification, timeouts, signed form tokens, origin checks, a honeypot, fixed email routing, and process-local request limits. Address searches use HERE; address/placement coordinates are sent to USGS for elevation checks. No customer details are stored in a database or browser storage, and local request logging is disabled. The quote is sent to the recipient's mailbox, so normal email retention applies there.

Process-local limits and duplicate guards are not distributed abuse protection or an exactly-once delivery guarantee. A stable ACS operation ID is reused on retries, but an uncertain provider response still requires checking the request reference/mailbox. For a broader public rollout, add verified anti-bot protection and durable submission reconciliation. Restrict HERE quotas and monitor usage during the interview demo.

Dimension sources: [Waste Haul](https://www.wastehaul.com/roll-off-dumpster-sizes), [Agri Service](https://agriserviceinc.com/delivery-services-wm/). Mapping: [HERE supplier terms and notices](https://legal.here.com/en-gb/terms/general-content-supplier-terms-and-notices).
