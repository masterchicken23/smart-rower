#include <WiFi.h>
#include <esp_now.h>

uint8_t receiverMAC[] = {0x44, 0xB1, 0x76, 0xCE, 0x40, 0xD0};

typedef struct {
  long distance_mm;
} SensorData;

SensorData data;

void onSent(const wifi_tx_info_t *tx_info, esp_now_send_status_t status) {
  Serial.println(status == ESP_NOW_SEND_SUCCESS ? "Sent OK" : "Send failed");
}

void setup() {
  Serial.begin(115200);
  Serial1.begin(115200, SERIAL_8N1, 44, -1);
  delay(3000);

  WiFi.mode(WIFI_STA);
  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    return;
  }

  esp_now_register_send_cb(onSent);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, receiverMAC, 6);
  peer.channel = 0;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
}

void loop() {
  if (Serial1.available()) {
    String line = Serial1.readStringUntil('\n');
    line.trim();

    int distIdx = line.indexOf("Distance: ");
    if (distIdx >= 0) {
      int mmIdx = line.indexOf(" mm", distIdx);
      if (mmIdx > distIdx) {
        String distStr = line.substring(distIdx + 10, mmIdx);
        data.distance_mm = distStr.toInt();

        esp_now_send(receiverMAC, (uint8_t *)&data, sizeof(data));

        Serial.print("Sent: ");
        Serial.print(data.distance_mm);
        Serial.println(" mm");
      }
    }
  }
}