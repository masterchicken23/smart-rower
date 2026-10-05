# MQTT interface — firmware spec

What a sensor device must publish for the funnel to accept it. The funnel
(`embedded/funnel/`) implements this side already; match it and the data shows
up at `GET /rower/<seat>/position` with no funnel changes.

Full rationale is in `README.md`. This is the implementation checklist.

---

## 1. Broker connection

| setting | value |
| --- | --- |
| host | the Jetson's address, port **1883** |
| TLS | none |
| auth | none (anonymous) |
| client ID | **`rower-<mac6>`** — must be unique |
| clean session | true |
| keepalive | 15 s |
| reconnect | retry forever, ~2 s backoff |

`<mac6>` is the last 3 bytes of the device MAC in lowercase hex, e.g. `ce40d0`.

> **Two clients sharing an ID will disconnect each other in a loop.** The
> broker kicks the older connection every time the newer one connects. Derive
> the ID from the MAC; never hard-code it.

Give the Jetson a static IP on the boat's AP, or resolve it by hostname. There
is no internet route and no certificate to validate.

---

## 2. Seat number — one per device

The seat number is **in the topic**, so each device has to know its own. Seats
are **1-based, bow = 1**; in an eight, stroke is 8. There is no seat 0.

### Store it in NVS, not in the binary

Use ESP32 NVS (`Preferences`), namespace `rower`, key `seat`, as a `uint8`.

One firmware image flashes to every device, the seat is data rather than code,
and it survives OTA updates. The alternative — a `#define` per device — means
eight binaries to keep straight and a laptop with a toolchain to move a sensor
between seats.

```cpp
#include <Preferences.h>
Preferences prefs;

uint8_t seat = 0;                       // 0 = unprovisioned

void loadSeat() {
  prefs.begin("rower", true);           // read-only
  seat = prefs.getUChar("seat", 0);
  prefs.end();
}

void saveSeat(uint8_t n) {
  prefs.begin("rower", false);
  prefs.putUChar("seat", n);
  prefs.end();
}
```

### Provision over serial

A two-command console is enough. Keep it in the production firmware so a seat
can be changed on the water with any serial terminal.

```
> seat          ->  seat=3 dev=ce40d0
> seat 5        ->  seat=5 saved, rebooting
```

Reject anything outside 1–16. Reboot after saving so every derived string
(topics, client ID) is rebuilt from one place.

### An unprovisioned device must be loud, not wrong

If `seat == 0`, **do not guess and do not publish any `rower/...` topic.** A
device that defaults to seat 1 silently corrupts a real seat's data.

Instead, publish a beacon to `boat/unassigned` **every 500 ms**, and blink an
LED:

```json
{"seq": 7, "t_ms": 14000, "unassigned": 1, "dev": "ce40d0"}
```

This shows up at `GET /boat/unassigned` with no funnel change, so an
unprovisioned device on the network is visible from the API.

> **Any topic's publish interval must be shorter than `FUNNEL_MAX_AGE_MS`
> (1 s), or it always reads as stale.** A 2 s beacon exists in `/streams` but
> `/boat/unassigned` answers `503` between publishes. The same rule applies to
> any slow stream you add later — either publish faster than the age limit or
> expect the per-resource endpoint to look stale.

### Do not let two devices claim one seat

The funnel cannot reject this — it will interleave both senders into one
stream. The symptom is `n_gap` on `/streams` climbing fast while `dev` flips
between two IDs. Check `GET /rowers` after any swap.

---

## 3. Topics

| topic | QoS | retain | when |
| --- | --- | --- | --- |
| `rower/<seat>/seat` | 0 | no | every measurement |
| `rower/<seat>/status` | 1 | **yes** | on connect, and as Last Will |
| `boat/unassigned` | 0 | no | every 500 ms, only if unprovisioned |

Lowercase, exactly these segment counts. Anything else — `rower/3/seat/raw`,
`ROWER/3/seat`, `rower/0/seat` — is rejected and counted in `unknown_topics`.
(A stray leading or trailing slash is tolerated, but do not rely on it.)

The last segment (`seat`) names the stream, so a second sensor on the same
rower is a new stream name — `rower/3/force` — and needs no funnel change to
start appearing.

---

## 4. Sample payload

One JSON object per publish, on `rower/<seat>/seat`:

```json
{"seq": 412, "t_ms": 183450, "d_mm": 812, "v_mms": -350, "dev": "ce40d0"}
```

| field | type | required | meaning |
| --- | --- | --- | --- |
| `seq` | uint32 | **yes** | counter, +1 per *measurement* |
| `t_ms` | uint32 | **yes** | `millis()` **at the moment of measurement** |
| `d_mm` | int | **yes** | distance in millimetres |
| `v_mms` | int | no | velocity, mm/s |
| `dev` | string | no | `<mac6>`; send it, it makes faults diagnosable |

`seq`, `t_ms`, `dev` (and optional `t`) are the envelope. **Any other key is
passed straight through to the app**, so adding a measurement needs no funnel
change — publish `"force_n": 231` and it appears in the API.

Three rules that are easy to get wrong:

1. **`t_ms` is sampled when the sensor fires, not when you publish.** Capture
   `millis()` next to the measurement and carry it through. Stamping at publish
   time bakes in WiFi latency — the exact defect in the current
   `sensor-esp-pipeline`, where `receiver.ino` restamps on packet arrival.

2. **`seq` counts measurements, not successful publishes.** Increment it even
   when you skip or fail a publish. That is what makes a dropped packet show up
   as `n_gap` instead of silently looking like a slower sensor.

3. **Never buffer and dump on reconnect.** Drop samples taken while
   disconnected. A burst of seconds-old readings is worse than a gap; the funnel
   stamps on arrival and would treat them all as current.

`seq` continues across a reconnect and resets to 0 only on reboot — the funnel
recognises a reset and does not charge it as lost samples. Wrapping at 2³² is
handled.

Publish at the sensor's natural rate (~20 Hz for the HC-SR04). No coordination
with the funnel is needed; it measures your actual rate and reports it.

---

## 5. Status payload and Last Will

On `rower/<seat>/status`, **QoS 1, retained**:

```json
{"up": true, "seat": 3, "dev": "ce40d0", "fw": "1.0", "hz": 20}
```

Set the Last Will to the same topic, also retained:

```json
{"up": false, "seat": 3, "dev": "ce40d0"}
```

The broker publishes the will if the device drops without a clean disconnect.
That is what separates "sensor is gone" from "sensor is connected but silent" —
sample age alone cannot tell those apart. It surfaces as `up` in `GET /rowers`.

Including `seat` in the payload is deliberate redundancy: it lets a mismatch
between the topic and the device's own belief be spotted.

### Retiring a seat — clear the retained status

**A retained message outlives the device that sent it.** Re-provision a sensor
from seat 3 to seat 5 and seat 3 keeps reporting `up: true` for ever, because
that payload is still sitting in the broker.

So before changing a device's seat — and before removing a sensor from the boat
— retract the old topic with a **zero-length retained publish**:

```sh
mosquitto_pub -h <jetson> -t rower/3/status -r -n
```

The funnel honours this as "forget seat 3" and drops it from `/rowers`. Doing
it in firmware immediately before `saveSeat()` is the reliable place:

```cpp
client.publish(tStatus, (const uint8_t *)"", 0, true);   // retain, empty
```

Note the difference from the Last Will: `{"up": false}` means *this seat's
sensor has dropped out*, and the seat stays listed. An empty payload means
*this seat no longer exists*.

---

## 6. Sketch skeleton (PubSubClient)

```cpp
char clientId[16], tSeat[24], tStatus[24];
uint32_t seq = 0;

void buildTopics() {
  snprintf(clientId, sizeof clientId, "rower-%s", mac6);
  snprintf(tSeat,    sizeof tSeat,    "rower/%u/seat",   seat);
  snprintf(tStatus,  sizeof tStatus,  "rower/%u/status", seat);
}

bool connect() {
  char will[64];
  snprintf(will, sizeof will,
           "{\"up\":false,\"seat\":%u,\"dev\":\"%s\"}", seat, mac6);

  // clientId, user, pass, willTopic, willQos, willRetain, willMsg, cleanSession
  if (!client.connect(clientId, NULL, NULL, tStatus, 1, true, will, true))
    return false;

  char up[80];
  snprintf(up, sizeof up,
           "{\"up\":true,\"seat\":%u,\"dev\":\"%s\",\"fw\":\"1.0\",\"hz\":20}",
           seat, mac6);
  client.publish(tStatus, up, true);     // retained
  return true;
}

void sampleAndPublish() {
  uint32_t t_ms = millis();              // BEFORE the measurement
  int d_mm = readDistanceMm();
  seq++;                                 // every measurement, published or not

  if (!client.connected()) return;       // drop it; do not queue

  char buf[96];
  snprintf(buf, sizeof buf,
           "{\"seq\":%lu,\"t_ms\":%lu,\"d_mm\":%d,\"dev\":\"%s\"}",
           (unsigned long)seq, (unsigned long)t_ms, d_mm, mac6);
  client.publish(tSeat, buf);            // QoS 0, not retained
}
```

> `PubSubClient` **silently fails** to publish anything longer than its buffer
> and just returns `false`. Call `client.setBufferSize(256)` after
> `setServer()`.

`snprintf` is fine at this size; ArduinoJson works too (a 128-byte
`JsonDocument` is ample).

---

## 7. Verifying a device

With the funnel running:

```sh
# is it arriving, at what rate, and is anything being lost?
curl -s localhost:8000/streams | jq '.[] | select(.path=="rower/3/seat")'
#   rate_hz ≈ your publish rate,  n_gap 0,  n_bad 0,  dev "ce40d0"

# presence, from the retained status topic
curl -s localhost:8000/rowers | jq

# the processed value the app reads
curl -s localhost:8000/rower/3/position | jq
```

What the counters mean when they are not zero:

| symptom | cause |
| --- | --- |
| `n_bad` rising | payload is not a JSON object, or has no measurement field |
| `n_gap` rising | packets lost in flight (weak link), or two devices on one seat |
| `unknown_topics` in `/health` | topic shape is wrong |
| seat missing from `/rowers` | never published; check provisioning and `boat/unassigned` |
| ghost seat, `up: true`, no streams | stale retained status from a device that moved; clear it |
| `503` from `/rower/3/position` | last sample older than 1 s |

`position` being `null` is expected: the funnel's transformation pipeline is
not implemented yet. `travel_mm` tracking your readings means the path works.

You can also test without hardware:

```sh
mosquitto_pub -h <jetson> -t rower/3/seat \
  -m '{"seq":1,"t_ms":1000,"d_mm":812,"dev":"test01"}'
```

---

## Checklist

- [ ] Client ID derived from MAC, unique per device
- [ ] Seat read from NVS; `seat` serial command to set it
- [ ] Unprovisioned (`seat == 0`) publishes only `boat/unassigned`, never `rower/...`
- [ ] `t_ms` captured at measurement, not at publish
- [ ] `seq` incremented per measurement, including skipped publishes
- [ ] Samples QoS 0, not retained; dropped rather than queued while disconnected
- [ ] Status retained QoS 1 on connect, with matching Last Will
- [ ] Old `rower/<seat>/status` cleared (empty retained publish) before changing seat
- [ ] `setBufferSize(256)`
- [ ] Reconnect loop that never gives up
