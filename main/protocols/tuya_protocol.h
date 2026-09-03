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

    // Timestamp of the last byte received from the server on any TAI stream.
    // A growing silence while we keep sending means the RX path stalled, which
    // is what the packet capture showed (frozen ACK, server retransmits, RST).
    std::atomic<int64_t> last_rx_ms_{0};
    int64_t last_diag_dump_ms_ = 0;
    void MarkRx();
    int64_t RxSilenceMs() const;

    // How long tai_send_audio_chunk actually blocks. lwIP returns ERR_MEM once
    // snd_queuelen hits TCP_SND_QUEUELEN (4*TCP_SND_BUF/TCP_MSS == 16 here) and
    // a blocking socket then retries, so a full send queue shows up as latency,
    // not as a failed send. These counters make that latency visible.
    int64_t send_us_max_ = 0;
    int64_t send_us_total_ = 0;
    int send_count_ = 0;
    int send_slow_count_ = 0;
    int64_t last_send_stat_ms_ = 0;

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
