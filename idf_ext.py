# idf.py extension: Tuya auth credential commands.
#
# idf.py loads this file automatically (project-root idf_ext.py).
# Provides:
#   idf.py tuya-auth-flash  - write tuya_authkey.txt to the device nvs partition
#   idf.py tuya-auth-read   - print the identity stored on the device
#
# Both erase/replace the whole nvs partition: WiFi credentials, device
# settings and the Tuya activation state are wiped; the device must re-pair
# with the Tuya app. See scripts/tuya_auth_tool.py for details.
import os
import subprocess
import sys
from typing import Dict

TOOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'scripts', 'tuya_auth_tool.py')


def _resolve_port(args, action_port):
    # -p/--port is an idf.py global option. When supplied before an action it
    # is stored in global args rather than in that action's option dict.
    port = action_port or getattr(args, 'port', None)
    if port:
        return port

    # Match idf.py's own serial actions: enumerate ports and probe them with
    # esptool until a connected ESP is found. Import lazily so the host tool's
    # direct unit tests do not need ESP-IDF on sys.path.
    from idf_py_actions.tools import get_default_serial_port
    return get_default_serial_port()


def _run_tool(action: str, ctx, args, port: str, extra: Dict) -> None:
    port = _resolve_port(args, port)
    cmd = [sys.executable, TOOL_PATH, action]
    if port:
        cmd += ['--port', port]
    for key, value in extra.items():
        if value is True:
            cmd += ['--%s' % key.replace('_', '-')]
        elif value is not None and value is not False:
            cmd += ['--%s' % key.replace('_', '-'), value]
    # idf.py runs inside the IDF python env, which has esp_idf_nvs_partition_gen.
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def action_extensions(base_actions: Dict, project_path: str) -> Dict:
    # idf.py invokes action callbacks as callback(action_name, ctx,
    # global_args, **action_options) — the leading name parameter is required,
    # otherwise every option lands in the wrong variable.
    def tuya_auth_flash(name, ctx, args, port=None, uuid=None, auth_key=None,
                        pid=None, file=None, no_verify=False) -> None:
        extra = {
            'uuid': uuid,
            'auth_key': auth_key,
            'pid': pid,
            'file': file,
            'no_verify': no_verify,
        }
        _run_tool('write', ctx, args, port, extra)

    def tuya_auth_read(name, ctx, args, port=None) -> None:
        _run_tool('read', ctx, args, port, {})

    return {
        'actions': {
            'tuya-auth-flash': {
                'callback': tuya_auth_flash,
                'short_help': 'Write Tuya credentials (tuya_authkey.txt) to the nvs partition. '
                              'Erases WiFi/settings/activation; the device must re-pair.',
                'dependencies': [],
                'options': [
                    {
                        'names': ['-p', '--port'],
                        'help': 'Serial port device.',
                    },
                    {
                        'names': ['--file'],
                        'help': 'Credential file to write (default: <project>/tuya_authkey.txt).',
                    },
                    {
                        'names': ['--uuid'],
                        'help': 'Override the uuid from the credential file.',
                    },
                    {
                        'names': ['--auth-key'],
                        'help': 'Override the auth key from the credential file.',
                    },
                    {
                        'names': ['--pid'],
                        'help': 'Override the product key from the credential file.',
                    },
                    {
                        'names': ['--no-verify'],
                        'is_flag': True,
                        'help': 'Skip read-back verification after writing.',
                    },
                ],
            },
            'tuya-auth-read': {
                'callback': tuya_auth_read,
                'short_help': 'Print the Tuya identity stored in the device nvs partition.',
                'dependencies': [],
                'options': [
                    {
                        'names': ['-p', '--port'],
                        'help': 'Serial port device.',
                    },
                ],
            },
        },
    }
