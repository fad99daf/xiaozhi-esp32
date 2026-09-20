#ifndef TUYA_VERSION_REPORT_STATE_H
#define TUYA_VERSION_REPORT_STATE_H

namespace TuyaVersionReportState {

bool ShouldReport(const char* current_version);
bool MarkPending(const char* target_version);
bool MarkReported(const char* current_version);

}  // namespace TuyaVersionReportState

#endif
