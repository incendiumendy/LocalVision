# Local LLM and camera failure monitoring

## Feasibility

The installed Moonraker camera `cam1` exposes:

- stream: `/webcam/?action=stream`
- snapshot: `/webcam/?action=snapshot`
- current snapshot format: JPEG, 640 x 480

The camera view includes the nozzle and a large part of the build surface, so
it is suitable for initial spaghetti/failure detection. Snapshot capture and
inference can remain entirely on the local network.

Local Vision uses a local vision-capable model through an OpenAI-compatible
HTTP API. A strict JSON response is required:

```json
{
  "failure_probability": 0.0,
  "failure_type": "none",
  "visible_evidence": [],
  "image_usable": true
}
```

The model output is advisory evidence, not a direct printer command.

## Decision pipeline

1. Read Moonraker print state and elapsed print time.
2. Request one camera snapshot at the configured interval.
3. Reject dark, stale, corrupt or obstructed images.
4. Combine the image with a compact telemetry summary:
   ALPS pressure trend, commanded extrusion, acceleration health,
   temperature, layer/time and recent anomalies.
5. Ask the local vision model for schema-constrained JSON.
6. Require the configured confidence on multiple consecutive snapshots.
7. Apply the configured policy once and write an audit event with the
   snapshots and evidence.

## Configurable reactions

| Dashboard policy | Behavior |
| --- | --- |
| Warn only | Record and display the event; no printer command |
| Pause | Pause only after the consecutive-frame gate |
| Cancel early, pause later | Cancel only while elapsed print time is within the configured early window; pause afterward |

Defaults are deliberately conservative:

- warn only;
- snapshot every 5 seconds;
- 85 percent minimum confidence;
- three consecutive detections;
- 20 minute early-cancel window if that policy is later selected.

The dashboard currently stores this policy locally but does not arm printer
actions. The monitor service, model endpoint and final action interlock must be
validated before enabling pause or cancel.

## Fail-safe rules

- Missing camera, model timeout, invalid JSON or missing telemetry never
  cancels or pauses a print.
- One weak frame never triggers an action.
- A cancel command is never issued outside the early-cancel window.
- After that window, the most severe permitted action is pause.
- Each action is idempotent and limited to once per incident.
- Every decision stores timestamp, print filename, elapsed time, confidence,
  reason, model identifier and snapshot hashes.
- A manual user resume clears the incident only after fresh good frames.

## Remaining connection data

To activate the monitor, configure:

- local LLM base URL reachable from the Raspberry Pi;
- exact vision-capable model identifier;
- optional local API token;
- an explicit user confirmation that printer actions may be armed.

No camera image should be sent to a public/cloud API by default.
