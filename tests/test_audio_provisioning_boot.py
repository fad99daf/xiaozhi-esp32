"""Host boot-order regression: real Initialize/Alert and WiFi startup methods.

Hardware, BLE, WiFi and audio are event-recording stubs, not memory models.
Run: python3 -m unittest discover -s tests -p 'test_audio_provisioning_boot.py' -v
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_audio_abort import method

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <functional>
#include <iostream>
#include <string>
#include <string_view>
#include <vector>

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define pdMS_TO_TICKS(ms) (ms)
constexpr int MAIN_EVENT_SEND_AUDIO = 1, MAIN_EVENT_WAKE_WORD_DETECTED = 2,
    MAIN_EVENT_VAD_CHANGE = 3, MAIN_EVENT_STATE_CHANGED = 4,
    MAIN_EVENT_NETWORK_DISCONNECTED = 5, MAIN_EVENT_NETWORK_CONNECTED = 6;
constexpr int ESP_BT_MODE_BLE = 1, CONNECT_TIMEOUT_SEC = 60;
void record(const std::string& event) { std::cout << event << '\n'; }
struct Restart {};
struct ErrorWait {};
bool ble_success = true;
void vTaskDelay(int ms) {
    record("delay:" + std::to_string(ms));
    if (ms == 1000) throw ErrorWait{};
}
[[noreturn]] void esp_restart() { record("restart"); throw Restart{}; }
void esp_bt_mem_release(int) { record("bt.release"); }
void esp_timer_start_once(void*, unsigned long long) { record("wifi.timer"); }
void esp_timer_start_periodic(void*, int) { record("clock.start"); }
void xEventGroupSetBits(void*, int) {}

namespace Lang {
const char* CODE = "en-US";
namespace Strings {
const char *ERROR = "error", *WIFI_CONFIG_MODE = "wifi-config",
    *ENTERING_WIFI_CONFIG_MODE = "entering-wifi-config", *SCANNING_WIFI = "scanning",
    *REGISTERING_NETWORK = "registering", *CONNECT_TO = "connect-to",
    *CONNECTED_TO = "connected-to", *DETECTING_MODULE = "detecting",
    *PIN_ERROR = "pin-error", *REG_ERROR = "reg-error", *MODEM_INIT_ERROR = "modem-error";
}
namespace Sounds {
const std::string_view OGG_WIFICONFIG = "wifi-config-sound", OGG_ERR_PIN = "pin-sound",
    OGG_ERR_REG = "reg-sound", OGG_EXCLAMATION = "error-sound";
}
}
struct SystemInfo { static std::string GetUserAgent() { return "test-board"; } };
struct Display {
    void SetupUI() { record("display.setup"); }
    void SetChatMessage(const char* role, const char* text) {
        record(std::string("chat:") + role + ":" + text);
    }
    void SetStatus(const char* text) { record(std::string("status:") + text); }
    void SetEmotion(const char* text) { record(std::string("emotion:") + text); }
    void ShowNotification(const char*, int) { record("notification"); }
    void UpdateStatusBar(bool) { record("status-bar"); }
};
struct Codec {};
struct AudioServiceCallbacks {
    std::function<void()> on_send_queue_available;
    std::function<void(const std::string&)> on_wake_word_detected;
    std::function<void(bool)> on_vad_change;
};
struct AudioService {
    void Initialize(Codec*) { record("audio.initialize"); }
    void Start() { record("audio.start"); }
    void SetCallbacks(AudioServiceCallbacks) { record("audio.callbacks"); }
    void PlaySound(const std::string_view& sound) { record("sound:" + std::string(sound)); }
};
enum DeviceState { kDeviceStateStarting, kDeviceStateWifiConfiguring };
struct StateMachine {
    void AddStateChangeListener(std::function<void(DeviceState, DeviceState)>) {
        record("state.listener");
    }
};
struct McpServer {
    static McpServer& GetInstance() { static McpServer server; return server; }
    void AddCommonTools() { record("mcp.common"); }
    void AddUserOnlyTools() { record("mcp.user"); }
};
enum class NetworkEvent { Scanning, Connecting, Connected, Disconnected,
    WifiConfigModeEnter, WifiConfigModeExit, ModemDetecting, ModemErrorNoSim,
    ModemErrorRegDenied, ModemErrorInitFailed, ModemErrorTimeout };
struct Board {
    static Board* instance;
    static Board& GetInstance() { return *instance; }
    virtual ~Board() = default;
    Display display;
    Codec codec;
    Display* GetDisplay() { return &display; }
    Codec* GetAudioCodec() { record("codec.get"); return &codec; }
    virtual void StartNetwork() { record("cellular.start"); }
    void SetNetworkEventCallback(std::function<void(NetworkEvent, const std::string&)>) {
        record("network.callback");
    }
};
Board* Board::instance = nullptr;
struct WifiBoard : Board {
    void* connect_timer_ = nullptr;
    void StartNetwork() override;
    void TryWifiConnect();
    void OnNetworkEvent(NetworkEvent, const std::string& = "") {}
    void StartWifiConfigMode() { record("wifi.config-mode"); }
};
struct SsidManager {
    std::vector<std::string> ssids;
    static SsidManager& GetInstance() { static SsidManager manager; return manager; }
    const auto& GetSsidList() { return ssids; }
    void AddSsid(const std::string& ssid, const std::string&) {
        record("ssid.save"); ssids.push_back(ssid);
    }
};
enum class WifiEvent { Scanning, Connecting, Connected, Disconnected,
    ConfigModeEnter, ConfigModeExit };
struct WifiManagerConfig { std::string ssid_prefix, language; };
struct WifiManager {
    int polls = 0;
    static WifiManager& GetInstance() { static WifiManager manager; return manager; }
    void Initialize(WifiManagerConfig) { record("wifi.initialize"); }
    void SetEventCallback(std::function<void(WifiEvent, const std::string&)>) {
        record("wifi.callback");
    }
    void StartStation() { record("wifi.station"); }
    bool IsConnected() {
        if (++polls == 1) return false;
        record("wifi.connected"); return true;
    }
};
struct BleProvResult { std::string ssid = "ssid", password = "password", token = "token"; };
bool TuyaBleProvision(int, BleProvResult&) { record("ble.provision"); return ble_success; }
struct TuyaProtocol {
    static bool OnBoardWithToken(const std::string&) { record("tuya.onboard"); return true; }
};
struct Application {
    AudioService audio_service_;
    StateMachine state_machine_;
    void* event_group_ = nullptr;
    void* clock_timer_handle_ = nullptr;
    static Application& GetInstance() { static Application app; return app; }
    bool SetDeviceState(DeviceState state) {
        record(state == kDeviceStateStarting ? "state.starting" : "state.wifi-configuring");
        return true;
    }
    void Initialize();
    void Alert(const char*, const char*, const char*, const std::string_view&);
};
// PRODUCTION_METHODS
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    const std::string scenario = argv[1];
    WifiBoard wifi;
    Board cellular;
    Board::instance = scenario == "cellular" ? &cellular : &wifi;
    if (scenario == "saved") SsidManager::GetInstance().ssids.push_back("saved");
    ble_success = scenario != "ble-failure";
    try {
        Application::GetInstance().Initialize();
        record("initialize.returned");
    } catch (const Restart&) {
        record("terminal.restart");
    } catch (const ErrorWait&) {
        record("terminal.error-wait");
    }
}
'''


class AudioProvisioningBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        application = (ROOT / 'main/application.cc').read_text()
        wifi = (ROOT / 'main/boards/common/wifi_board.cc').read_text()
        methods = '\n'.join((
            method(application, 'Application::Initialize('),
            method(application, 'Application::Alert('),
            method(wifi, 'WifiBoard::StartNetwork('),
            method(wifi, 'WifiBoard::TryWifiConnect('),
        ))
        cls.temp = tempfile.TemporaryDirectory(dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        path = Path(cls.temp.name)
        source = path / 'boot.cc'
        source.write_text(HARNESS.replace('// PRODUCTION_METHODS', methods))
        cls.executables = {}
        for name, flags in (
            ('c3_ble', ['CONFIG_IDF_TARGET_ESP32C3', 'CONFIG_TUYA_BLE_PROVISIONING']),
            ('s3_ble', ['CONFIG_IDF_TARGET_ESP32S3', 'CONFIG_TUYA_BLE_PROVISIONING']),
            ('c3_no_ble', ['CONFIG_IDF_TARGET_ESP32C3']),
            ('s3_no_ble', ['CONFIG_IDF_TARGET_ESP32S3']),
        ):
            executable = path / name
            subprocess.run(['c++', '-std=c++17', '-O1', '-g',
                            '-fsanitize=address,undefined',
                            *['-D' + flag + '=1' for flag in flags],
                            str(source), '-o', str(executable)], check=True, timeout=60)
            cls.executables[name] = executable

    def boot(self, configuration, scenario):
        result = subprocess.run([str(self.executables[configuration]), scenario],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.splitlines()

    def assert_normal_startup(self, events):
        self.assertEqual(events[:12], [
            'state.starting', 'display.setup', 'chat:system:test-board',
            'codec.get', 'audio.initialize', 'audio.start', 'audio.callbacks',
            'state.listener', 'clock.start', 'mcp.common', 'mcp.user', 'network.callback',
        ])
        for event in ('codec.get', 'audio.initialize', 'audio.start'):
            self.assertEqual(events.count(event), 1)

    def test_saved_ssid_preserves_normal_startup_for_all_configurations(self):
        for configuration in self.executables:
            with self.subTest(configuration=configuration):
                events = self.boot(configuration, 'saved')
                self.assert_normal_startup(events)
                self.assertIn('wifi.station', events)
                self.assertNotIn('ble.provision', events)
                self.assertNotIn('tuya.onboard', events)
                self.assertEqual(events[-2:], ['status-bar', 'initialize.returned'])

    def test_first_boot_keeps_audio_and_provisioning_prompt(self):
        for configuration in ('c3_ble', 's3_ble'):
            with self.subTest(configuration=configuration):
                events = self.boot(configuration, 'first')
                self.assert_normal_startup(events)
                self.assertEqual(events.count('sound:wifi-config-sound'), 1)
                milestones = ['sound:wifi-config-sound', 'ble.provision', 'ssid.save',
                              'wifi.station', 'wifi.connected', 'tuya.onboard', 'restart']
                positions = [events.index(event) for event in milestones]
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(events[-2:], ['restart', 'terminal.restart'])
                self.assertNotIn('initialize.returned', events)

    def test_failed_ble_preserves_return_to_normal_startup(self):
        for configuration in ('c3_ble', 's3_ble'):
            with self.subTest(configuration=configuration):
                events = self.boot(configuration, 'ble-failure')
                self.assert_normal_startup(events)
                self.assertEqual(events.count('sound:wifi-config-sound'), 1)
                self.assertLess(events.index('sound:wifi-config-sound'),
                                events.index('ble.provision'))
                for event in ('ssid.save', 'wifi.station', 'tuya.onboard', 'restart'):
                    self.assertNotIn(event, events)
                self.assertEqual(events[-2:], ['status-bar', 'initialize.returned'])

    def test_provisioning_disabled_keeps_normal_first_boot(self):
        for configuration in ('c3_no_ble', 's3_no_ble'):
            with self.subTest(configuration=configuration):
                events = self.boot(configuration, 'first')
                self.assert_normal_startup(events)
                self.assertNotIn('ble.provision', events)
                self.assertEqual(events[-4:], [
                    'delay:1500', 'wifi.config-mode', 'status-bar', 'initialize.returned'])

    def test_non_wifi_board_does_not_enter_early_provisioning(self):
        events = self.boot('c3_ble', 'cellular')
        self.assert_normal_startup(events)
        self.assertNotIn('ble.provision', events)
        self.assertEqual(events[-3:], ['cellular.start', 'status-bar', 'initialize.returned'])


if __name__ == '__main__':
    unittest.main()
