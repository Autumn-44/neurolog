# Wonderlust live logging

Wonderlust's `app.js` uses `utils/neurolog.js` to send actual request status, duration,
route templates, request error types and database startup events to NeuroLog.
The source name is `wonderlust`. Query strings, request bodies, headers and
actual URL identifiers are excluded.

Local defaults: `http://127.0.0.1:5001/api/ingest`; a local ingestion key is
provisioned automatically. Start NeuroLog, then run `npm start` in Wonderlust.
Open `http://127.0.0.1:5173/dashboard` and select Wonderlust.

Delivery runs asynchronously with a maximum of 1,000 queued events and retries
up to every 30 seconds. The queue is in memory and does not survive shutdown.
Acknowledgment failures can result in duplicates. New events are dropped with a
console warning when the queue fills.

The verified local startup currently reports MongoDB `ENOTFOUND`. Correct
`ATLASDB_URL` privately in Wonderlust's `.env` before expecting HTTP traffic.
The actual database failure was received by NeuroLog during verification.

For Render, deploy the modified Wonderlust application and set:

```
NEUROLOG_API_URL=https://neurolog-ai.onrender.com/api/ingest
NEUROLOG_INGEST_KEY=<key generated in the hosted NeuroLog workspace>
```

Store the key in Render environment settings, never in source control.
Production sending requires an explicit endpoint. The public Render workspace
allows others to view received metadata. Hosted deployment and remote ingestion
have not been activated or verified by this local installation.

The hosted NeuroLog application also preconnects this repository at startup,
including after its temporary storage is reset. Imports remain manual.

The repository `shahrukh-210906/wonderlust` is connected to the local GitHub
Actions importer. At connection time, GitHub returned no failed workflow runs.

Run `node --test integrations/wonderlust/neurolog.test.js` in NeuroLog to verify
privacy filtering and recovery from a delivery outage. Run `install.ps1` again
with `-WonderlustRoot <project-folder>` to copy an updated adapter into
Wonderlust; app edits are idempotent.
