#ifndef TUYA_PROTOCOL_H
#define TUYA_PROTOCOL_H

#include "protocol.h"
extern "C" {
    #include "tuya_ai.h"
    #include "iot_client.h"
}
#include <mutex>
#include <atomic>
#include <vector>

class TuyaProtocol : public Protocol {
public:
    TuyaProtocol();
    ~TuyaProtocol() override;

    bool Start() override;

    // Activate device on Tuya cloud using BLE provisioning token.
    // Requires WiFi to be connected. Saves credentials (devid, secret_key,
    // local_key) to NVS so subsequent boots skip on-boarding.
    // Returns true on success.
    static bool OnBoardWithToken(const std::string& token);

    bool OpenAudioChannel() override;
    void CloseAudioChannel(bool send_goodbye = true) override;
    bool IsAudioChannelOpened() const override;
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    void SendStartListening(ListeningMode mode) override;
    void SendStopListening() override;
    void SendAbortSpeaking(AbortReason reason) override;
    void SendMcpMessage(const std::string& payload) override;

private:
    iot_client_t* iot_client_ = nullptr;
    char* token_ = nullptr;
    std::string local_key_;  // from NVS after on-boarding

    struct ConnParams {
        char host[256] = {};
        char tls_sni[256] = {};
        char derived_client_id[256] = {};
        char agent_token[256] = {};
        uint16_t port = 0;
        long biz_code = 0;
        long biz_tag = 0;
    };
    ConnParams conn_params_;

    void* ctx_mem_ = nullptr;
    tai_ctx_t* ctx_ = nullptr;

    std::mutex send_mutex_;
    std::atomic<bool> connected_{false};
    std::atomic<bool> session_active_{false};
    bool is_first_audio_packet_ = true;
    bool has_received_first_nlg_ = false;
    std::atomic<bool> audio_end_pending_{false};
    int audio_recv_count_ = 0;
    std::vector<uint8_t> audio_reassembly_buf_;

    int connect_fail_count_ = 0;
    static const int MAX_CONNECT_FAILS_BEFORE_REFRESH = 2;

    bool InitIotClient();
    bool FetchToken();
    bool ParseToken();
    bool BuildTaiContext();
    bool RefreshTaiContext();

    static void OnAudioCb(tai_ctx_t* ctx, const tai_audio_msg_t* msg,
                          void* user);
    static void OnTextCb(tai_ctx_t* ctx, const tai_text_msg_t* msg,
                         void* user);
    static void OnEventCb(tai_ctx_t* ctx, const tai_event_msg_t* msg,
                          void* user);
    static void OnDisconnectCb(tai_ctx_t* ctx, const tai_disconnect_msg_t* msg,
                               void* user);

    void HandleAudio(const uint8_t* data, size_t len,
                     uint32_t sample_rate, uint16_t frame_duration);
    void HandleText(const char* text, size_t len, uint8_t stream_flag);
    void HandleEvent(uint16_t event_type, const uint8_t* data, size_t len);
    void HandleDisconnect(uint8_t reason, uint8_t detail,
                          uint16_t close_code, uint8_t connection_alive);

    bool SendText(const std::string& text) override;
};

#endif
