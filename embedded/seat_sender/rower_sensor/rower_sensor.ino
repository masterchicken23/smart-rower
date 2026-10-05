/*
 * rower_sensor.ino — HC-SR04 seat-position sensor for the MQTT funnel
 *
 * Implements FIRMWARE.md ("MQTT interface — firmware spec"). Section numbers
 * in the comments (§2, §5, ...) refer to that document.
 *
 *   Board    any ESP32, Arduino-ESP32 core 2.x or 3.x
 *   Library  PubSubClient (Nick O'Leary) >= 2.8
 *   Sensor   generic HC-SR04
 *
 * Wiring
 *   HC-SR04 VCC   -> 5V
 *   HC-SR04 GND   -> GND
 *   HC-SR04 TRIG  -> TRIG_PIN          (a 3.3 V trigger works fine)
 *   HC-SR04 ECHO  -> ECHO_PIN through a divider, e.g. 1k in series, 2k to GND.
 *                    ECHO swings to 5 V and the ESP32 is not 5 V tolerant.
 *
 * Provisioning (§2)
 *   A freshly flashed device has no seat: it fast-blinks the LED and publishes
 *   only boat/unassigned. Open a serial terminal at 115200 baud and type
 *     seat        ->  seat=3 dev=ce40d0
 *     seat 5      ->  seat=5 saved, rebooting
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <esp_mac.h>

// ============================================================================
// Configuration: fill these in before flashing
// ============================================================================

// Boat access point
static const char WIFI_SSID[]     = "Hotspot_H1A";
static const char WIFI_PASSWORD[] = "wifipassword";

// MQTT broker on the Jetson. Prefer its static IP on the boat's AP; a
// hostname also works if the AP's DNS resolves it.
static const char     MQTT_HOST[] = "10.74.228.133";
static const uint16_t MQTT_PORT   = 1883;

// Pins
static const uint8_t TRIG_PIN        = 5;
static const uint8_t ECHO_PIN        = 18;    // via the 5 V -> 3.3 V divider
static const uint8_t LED_PIN         = 2;     // onboard LED on most ESP32 DevKits
static const bool    LED_ACTIVE_HIGH = true;

// Sensor
static const uint32_t SAMPLE_PERIOD_MS   = 50;     // 20 Hz; reported as "hz" in status
static const uint32_t SPEED_OF_SOUND_M_S = 343;    // dry air at ~20 C
static const uint32_t ECHO_TIMEOUT_US    = 30000;  // > 4 m round trip; longer = no echo

static const char FW_VERSION[] = "1.0";

// If WiFi has been down this long, drop the association and start over rather
// than relying on the core's auto-reconnect alone.
static const uint32_t WIFI_REJOIN_MS = 15000;

// Period of the serial stats line, for checking a device on the bench. 0 = off.
static const uint32_t STATS_PERIOD_MS = 10000;

// ============================================================================
// Fixed by the spec: change only together with the funnel
// ============================================================================

static const uint16_t MQTT_KEEPALIVE_S = 15;     // §1
static const uint32_t MQTT_RETRY_MS    = 2000;   // §1: retry forever, ~2 s apart
static const uint32_t BEACON_PERIOD_MS = 500;    // §2: must stay under FUNNEL_MAX_AGE_MS (1 s)
static const uint8_t  SEAT_MIN = 1;              // §2: 1-based, bow = 1
static const uint8_t  SEAT_MAX = 16;
static const char     TOPIC_UNASSIGNED[] = "boat/unassigned";

// ============================================================================
// State
// ============================================================================

WiFiClient   net;
PubSubClient mqtt(net);
Preferences  prefs;

uint8_t  seat = 0;          // 0 = unprovisioned
char     mac6[7];           // last 3 MAC bytes, lowercase hex
char     clientId[16];      // rower-<mac6>
char     tSeat[24];         // rower/<seat>/seat
char     tStatus[24];       // rower/<seat>/status
uint32_t seq = 0;           // +1 per measurement (or beacon); resets only on reboot
uint32_t nextTickMs = 0;

// Local counters, printed on the serial stats line only
uint32_t nSent = 0, nPubFail = 0, nNoEcho = 0;
long     lastDmm = -1;

void setLed(bool on) {
  digitalWrite(LED_PIN, on == LED_ACTIVE_HIGH ? HIGH : LOW);
}

// ============================================================================
// Identity and seat storage (§1, §2)
// ============================================================================

void loadSeat() {
  prefs.begin("rower", true);                 // read-only
  seat = prefs.getUChar("seat", 0);
  prefs.end();
  if (seat > SEAT_MAX) {                      // corrupt or foreign value: don't guess
    Serial.printf("nvs: stored seat %u is out of range, ignoring\n", (unsigned)seat);
    seat = 0;
  }
}

void saveSeat(uint8_t n) {
  prefs.begin("rower", false);
  prefs.putUChar("seat", n);
  prefs.end();
}

// Every derived string is built here, once, at boot. Changing the seat
// reboots instead of patching these in place.
void buildIdentity() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(mac6, sizeof mac6, "%02x%02x%02x", mac[3], mac[4], mac[5]);
  snprintf(clientId, sizeof clientId, "rower-%s", mac6);
  if (seat) {
    snprintf(tSeat,   sizeof tSeat,   "rower/%u/seat",   (unsigned)seat);
    snprintf(tStatus, sizeof tStatus, "rower/%u/status", (unsigned)seat);
  }
}

// ============================================================================
// Sensor
// ============================================================================

// One HC-SR04 ping. Returns the echo pulse width in microseconds, or 0 if no
// echo arrived within ECHO_TIMEOUT_US. Blocks for at most that long.
uint32_t pingEchoUs() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  return pulseIn(ECHO_PIN, HIGH, ECHO_TIMEOUT_US);
}

// ============================================================================
// Network
// ============================================================================

// Provisioned: Last Will on our status topic, then announce up (§5).
// Unprovisioned: no will, and nothing under rower/ at all (§2).
bool mqttConnect() {
  bool ok;
  if (seat) {
    char will[64];
    snprintf(will, sizeof will, "{\"up\":false,\"seat\":%u,\"dev\":\"%s\"}",
             (unsigned)seat, mac6);
    // id, user, pass, willTopic, willQos, willRetain, willMsg, cleanSession
    ok = mqtt.connect(clientId, nullptr, nullptr, tStatus, 1, true, will, true);
  } else {
    ok = mqtt.connect(clientId, nullptr, nullptr, nullptr, 0, false, nullptr, true);
  }
  if (!ok) {
    Serial.printf("mqtt: connect to %s:%u failed (state %d)\n",
                  MQTT_HOST, (unsigned)MQTT_PORT, mqtt.state());
    return false;
  }

  if (seat) {
    char up[96];
    snprintf(up, sizeof up,
             "{\"up\":true,\"seat\":%u,\"dev\":\"%s\",\"fw\":\"%s\",\"hz\":%lu}",
             (unsigned)seat, mac6, FW_VERSION,
             (unsigned long)(1000 / SAMPLE_PERIOD_MS));
    // Retained. PubSubClient can only publish at QoS 0 (the will is QoS 1).
    // Over TCP this is lost only if the connection dies right here, and in
    // that case the broker publishes the will, which is then the truth.
    if (!mqtt.publish(tStatus, up, true)) {
      Serial.println("mqtt: status publish failed, reconnecting");
      mqtt.disconnect();
      return false;
    }
  }
  Serial.printf("mqtt: connected as %s\n", clientId);
  return true;
}

void maintainWifi(uint32_t now) {
  static bool     wasUp     = false;
  static uint32_t downSince = 0;

  bool up = WiFi.status() == WL_CONNECTED;
  if (up != wasUp) {
    if (up) Serial.printf("wifi: connected, ip %s, rssi %d dBm\n",
                          WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
    else    Serial.println("wifi: disconnected");
    wasUp = up;
    downSince = now;
  }
  if (!up && now - downSince >= WIFI_REJOIN_MS) {
    Serial.println("wifi: still down, rejoining");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    downSince = now;
  }
}

// §1: retry forever, ~2 s between attempts. An attempt blocks (TCP connect
// plus CONNACK, a few seconds when the broker is unreachable) and sampling
// pauses for that time; no measurement is taken, so seq does not advance.
void maintainMqtt() {
  static bool     wasConnected   = false;
  static bool     attempted      = false;
  static uint32_t lastAttemptEnd = 0;

  if (mqtt.connected()) {
    mqtt.loop();
    wasConnected = true;
    return;
  }
  if (wasConnected) {
    Serial.printf("mqtt: connection lost (state %d)\n", mqtt.state());
    wasConnected = false;
  }
  if (WiFi.status() != WL_CONNECTED) return;
  if (attempted && millis() - lastAttemptEnd < MQTT_RETRY_MS) return;

  mqttConnect();
  attempted = true;
  lastAttemptEnd = millis();   // back off from the end of a possibly slow attempt
}

// ============================================================================
// Serial console (§2)
// ============================================================================

// §5: before leaving a seat, clear its retained status with an empty retained
// publish, then disconnect *cleanly*. An unclean drop (such as just rebooting)
// makes the broker publish our retained Last Will, which would bring the seat
// straight back as {"up":false}.
bool retractStatus() {
  if (!mqtt.connected() && WiFi.status() == WL_CONNECTED) mqttConnect();
  if (!mqtt.connected()) return false;

  bool ok = mqtt.publish(tStatus, (const uint8_t *)"", 0, true);
  mqtt.loop();
  mqtt.disconnect();           // sends DISCONNECT, so the broker discards the will
  return ok;
}

void rebootNow() {
  if (mqtt.connected()) mqtt.disconnect();
  Serial.flush();
  delay(300);                  // let the TCP stack put the last packets on the wire
  ESP.restart();
}

void printSeat() {
  Serial.printf("seat=%u dev=%s%s\n", (unsigned)seat, mac6,
                seat ? "" : " (unprovisioned)");
}

void setSeat(uint8_t n) {
  if (n == seat) {
    Serial.printf("seat=%u unchanged\n", (unsigned)n);
    return;
  }
  if (seat && !retractStatus()) {
    Serial.printf("warning: broker unreachable, rower/%u/status NOT cleared.\n"
                  "         Clear it by hand: mosquitto_pub -h %s -t rower/%u/status -r -n\n",
                  (unsigned)seat, MQTT_HOST, (unsigned)seat);
  }
  saveSeat(n);
  Serial.printf("seat=%u saved, rebooting\n", (unsigned)n);
  rebootNow();
}

void handleCommand(char *s) {
  while (*s == ' ' || *s == '\t') s++;                        // trim
  char *end = s + strlen(s);
  while (end > s && (end[-1] == ' ' || end[-1] == '\t')) *--end = '\0';

  if (strcmp(s, "seat") == 0) {
    printSeat();
    return;
  }
  if (strncmp(s, "seat ", 5) == 0) {
    char *arg = s + 5;
    while (*arg == ' ') arg++;
    char *ep;
    long n = strtol(arg, &ep, 10);
    if (ep == arg || *ep != '\0' || n < SEAT_MIN || n > SEAT_MAX) {
      Serial.printf("error: seat must be %u-%u\n", (unsigned)SEAT_MIN, (unsigned)SEAT_MAX);
      return;
    }
    setSeat((uint8_t)n);
    return;
  }
  Serial.printf("commands: seat | seat <%u-%u>\n", (unsigned)SEAT_MIN, (unsigned)SEAT_MAX);
}

void pollSerial() {
  static char   line[32];
  static size_t len = 0;
  static bool   overflow = false;

  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r' || c == '\n') {
      if (len && !overflow) {
        line[len] = '\0';
        handleCommand(line);
      }
      len = 0;
      overflow = false;
    } else if (len < sizeof line - 1) {
      line[len++] = c;
    } else {
      overflow = true;           // discard over-long lines whole
    }
  }
}

// ============================================================================
// Publishing (§3, §4)
// ============================================================================

// One measurement, published raw: no filtering, no derived velocity.
void sampleAndPublish() {
  uint32_t t_ms    = millis();        // when the sensor fires, not when we publish
  uint32_t echo_us = pingEchoUs();
  seq++;                              // every measurement, published or not

  if (echo_us == 0) {                 // no echo: there is no distance to report.
    nNoEcho++;                        // seq has advanced, so the funnel sees a gap
    return;                           // rather than a made-up reading.
  }
  // Half the round trip: echo_us * (c / 1000 mm/us) / 2, rounded.
  long d_mm = (long)((echo_us * SPEED_OF_SOUND_M_S + 1000) / 2000);
  lastDmm = d_mm;

  if (!mqtt.connected()) return;      // drop it; never queue for later

  // echo_us is the untouched sensor value; the funnel passes extra keys
  // through, so the app can apply its own speed-of-sound correction.
  char buf[128];
  snprintf(buf, sizeof buf,
           "{\"seq\":%lu,\"t_ms\":%lu,\"d_mm\":%ld,\"echo_us\":%lu,\"dev\":\"%s\"}",
           (unsigned long)seq, (unsigned long)t_ms, d_mm,
           (unsigned long)echo_us, mac6);
  if (mqtt.publish(tSeat, buf)) nSent++;      // QoS 0, not retained
  else                          nPubFail++;
}

// Unprovisioned: be loud on boat/unassigned and publish nothing under rower/.
void publishBeacon(uint32_t now) {
  seq++;
  if (!mqtt.connected()) return;

  char buf[80];
  snprintf(buf, sizeof buf,
           "{\"seq\":%lu,\"t_ms\":%lu,\"unassigned\":1,\"dev\":\"%s\"}",
           (unsigned long)seq, (unsigned long)now, mac6);
  if (mqtt.publish(TOPIC_UNASSIGNED, buf)) nSent++;
  else                                     nPubFail++;
}

// ============================================================================
// Indicators
// ============================================================================

// Unprovisioned: fast blink, always. Provisioned: lit while connected to the broker.
void updateLed(uint32_t now) {
  setLed(seat ? mqtt.connected() : ((now / 125) & 1));
}

void logStats(uint32_t now) {
  static uint32_t last = 0;
  if (STATS_PERIOD_MS == 0 || now - last < STATS_PERIOD_MS) return;
  last = now;
  Serial.printf("[%s seat=%u] wifi=%s rssi=%d mqtt=%s seq=%lu sent=%lu "
                "pub_fail=%lu no_echo=%lu last_d_mm=%ld\n",
                mac6, (unsigned)seat,
                WiFi.status() == WL_CONNECTED ? "up" : "down", (int)WiFi.RSSI(),
                mqtt.connected() ? "up" : "down",
                (unsigned long)seq, (unsigned long)nSent, (unsigned long)nPubFail,
                (unsigned long)nNoEcho, lastDmm);
  if (!seat) Serial.println("UNPROVISIONED: set with `seat <1-16>`");
}

// ============================================================================

void setup() {
  Serial.begin(115200);

  pinMode(TRIG_PIN, OUTPUT);
  digitalWrite(TRIG_PIN, LOW);
  pinMode(ECHO_PIN, INPUT_PULLDOWN);  // unplugged sensor reads as "no echo", not noise
                                      // (GPIO 34-39 have no pulldown; use the divider's)
  pinMode(LED_PIN, OUTPUT);
  setLed(false);

  loadSeat();
  buildIdentity();
  Serial.printf("\nrower sensor fw %s, dev %s, seat %u\n",
                FW_VERSION, mac6, (unsigned)seat);
  if (!seat) Serial.println("UNPROVISIONED: publishing boat/unassigned only. "
                            "Set with `seat <1-16>`");

  WiFi.persistent(false);             // don't rewrite credentials to flash every boot
  WiFi.setHostname(clientId);         // shows as rower-<mac6> in the AP's client list
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);               // modem sleep adds latency and jitter to publishes
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(256);            // larger publishes would fail silently (§6)
  mqtt.setKeepAlive(MQTT_KEEPALIVE_S);

  nextTickMs = millis();
}

void loop() {
  pollSerial();
  maintainWifi(millis());
  maintainMqtt();

  uint32_t now = millis();
  if ((int32_t)(now - nextTickMs) >= 0) {
    uint32_t period = seat ? SAMPLE_PERIOD_MS : BEACON_PERIOD_MS;
    nextTickMs += period;
    if ((int32_t)(now - nextTickMs) >= 0)   // fell behind (e.g. a slow connect):
      nextTickMs = now + period;            // skip ahead, never burst to catch up
    if (seat) sampleAndPublish();
    else      publishBeacon(now);
  }

  updateLed(now);
  logStats(now);
}
