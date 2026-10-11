# Real-time detection and identity tracking

The camera performs one pose inference per frame. Camera capture already keeps
only the latest frame, and HTTP event delivery remains on a separate sender thread.
Face verification now also runs on a separate worker with one pending crop per
seat. Neither network requests nor face inference run in the video processing path.

## Stable names

`PersonTracker` assigns persistent IDs using minimum-cost one-to-one matching.
It combines torso position, motion prediction, box overlap and a small shirt-colour
histogram. The torso anchor does not move upward when a raised hand enlarges the
person box. Appearance templates are not updated while detected people overlap.

After two observations, an anonymous track can acquire a name from a calibrated,
present, assigned seat. That binding stays with the track even when it moves across
another seat. New detections cannot take a name reserved by a missing track.
Tracks survive up to three seconds without an observation. Indistinguishable
overlaps suspend labels until tracking evidence separates the people; they do not
assign names by detection order. Camera restart, changed roster assignments and
changed image dimensions invalidate the corresponding tracking state.

Initial names still depend on correct seat calibration. Identical or ambiguous
seat regions leave people unassigned instead of guessing. Clothing colour is a
tracking cue, not biometric proof. Long absences, identical clothing and complete
occlusion can require reacquisition; no camera tracker can guarantee identity when
the image provides no distinguishing evidence.

## Hand status and timing

Exactly one eligible student gets a green box and `Name | HAND RAISED`.
Other raised hands get blue boxes and `Name | 2nd Hand Raised`, `Name | 3rd Hand
Raised`, and so on. Lowered hands show `Standby` with a blue box. Unassigned people
show `Unassigned | Hand Raised (Unassigned)` and cannot receive
priority or enter the queue. Inactive sessions retain standby display, double-hand
raises remain orange, and detected face mismatches remain red.

The gesture filter records the first observed raise time for each persistent
person ID and preserves it through short keypoint gaps. One monotonic camera clock
is mapped to epoch timestamps, so a wall-clock adjustment cannot reorder raises.
Confirmation, face verification and HTTP delivery delays do not overwrite onset
times. Candidates are ranked after every person in the frame has been evaluated;
one immutable display state supplies that person's label and box colour together.
Events from the completed frame are delivered in onset order.

Both camera and dashboard queues sort by observed timestamp, then stable seat ID
for exact ties. This tie rule is independent of bounding-box position, roster order
and detector order. Raises first observed in one frame cannot be physically ordered
more finely than that frame. A brief missing leader retains priority during the
existing queue grace. Lowering or losing the first hand does not promote anyone:
the other students keep their original question ordinals and blue indicators.

The camera retains each student's first offer for the question, including students
who lower and raise again. New offers take the next ordinal, without filling the
places of students who have lowered. Once all participants are visibly down for
250 ms, the question order and gesture timers reset automatically. Missing people,
missing wrists, a missing camera and brief all-down flicker do not trigger this
reset. Session totals are retained. The dashboard honours the camera's ordinals
and restores saved ordinals after reconnects/restarts.

The gesture filter needs two raise samples at least 30 ms apart. It tolerates 80 ms
of a lowered posture and 200 ms of missing arm keypoints before changing the
visual state. The event state machine then uses a 200 ms confirmation window and
a 50 ms release grace. A completely missing person has a 500 ms queue grace, so
one dropped detection does not remove their place. The existing 1.5 second counter
cooldown remains independent of the visual state. These are algorithm thresholds,
not measured webcam or end-to-end latency guarantees.

Pose checks use person-relative height thresholds and correct image aspect ratio
for angles. Arms are checked independently: a resting opposite arm does not cancel
a genuine single-arm raise. Both genuinely raised arms still produce the existing
double-hand warning.

## Face verification

When enrolled embeddings and face models are available, confirmation waits for
the initial asynchronous verification attempt. Later confirmations also wait
while an earlier candidate's check is pending, so the dashboard never awards
temporary priority to a later verified person. Visual labels continue updating
without waiting for verification. Missing faces are reported as
unverified and retried; they are not permanently cached as mismatches. Results are
scoped to seat, track ID and enrollment data, expire after 1.6 seconds, and refresh
at most once every 800 ms per person. The face crop follows the head landmarks,
and face selection uses the tracked nose location to avoid choosing a neighbour.
Verification metadata is carried through ingestion to the dashboard ledger.
The dashboard advertises event schema version 2 through settings and socket
initialization. The camera sends verification metadata only when that capability
is advertised, so existing hosted dashboards continue accepting gesture events
during rollout. Updating the dashboard enables the additional ledger metadata.

## Regression checks without a group

From the repository root:

```powershell
python -m pytest desktop-app/tests -q
python -m pytest web-dashboard/tests/test_live_session.py web-dashboard/tests/test_camera_without_key.py -q
```

The desktop simulations exercise 2, 4 and 8 simultaneous detections, reordered
detections, changing raised-hand box shapes, crossings, overlaps, missing people,
moving-person reacquisition, reserved names, status colours, camera loss, clock
adjustments, lighting changes and slow/missing/mismatched face verification.
Priority checks include five simultaneous raisers, no promotion after release,
reversed roster order, earlier raises from a later seat, all same-timestamp insertion
permutations, delayed first-person verification, queue restoration, late offers,
ordinal suffixes and automatic new-question reset after all hands lower.
Camera protocol checks also cover older dashboards, reconnect negotiation and
ordered event delivery. The simulations run without YOLO
weights or a webcam; pose inference itself and actual classroom accuracy still
need recorded footage or a real camera session.

These changes are source changes. Previously built executables and installers
must be rebuilt to include the new tracker and workers.
