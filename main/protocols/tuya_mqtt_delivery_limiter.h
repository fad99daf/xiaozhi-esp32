#ifndef TUYA_MQTT_DELIVERY_LIMITER_H
#define TUYA_MQTT_DELIVERY_LIMITER_H

#include <atomic>

// Bounds full MQTT payloads waiting for the application task. Never block the
// SDK's MQTT receive loop; excess messages are dropped with a diagnostic.
class TuyaMqttDeliveryLimiter {
public:
    static constexpr unsigned kMaxPending = 2;

    bool TryAcquire() {
        unsigned pending = pending_.load();
        while (pending < kMaxPending) {
            if (pending_.compare_exchange_weak(pending, pending + 1)) return true;
        }
        return false;
    }

    void Release() { pending_.fetch_sub(1); }

private:
    std::atomic<unsigned> pending_{0};
};

#endif
