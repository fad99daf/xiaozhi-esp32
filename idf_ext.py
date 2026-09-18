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
import signal
import subprocess
import sys
from typing import Dict

TOOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'scripts', 'tuya_auth_tool.py')
BATCH_TOOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'scripts', 'batch_flash.py')
BATCH_REQUIREMENTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'scripts', 'requirements-batch-flash.txt')
BATCH_DEPENDENCY_IMPORTS = ('openpyxl', 'serial')


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


def _run_batch_setup() -> None:
    print('Installing batch flash dependencies with: %s' % sys.executable)
    result = subprocess.run([
        sys.executable, '-m', 'pip', 'install', '-r', BATCH_REQUIREMENTS_PATH,
    ])
    if result.returncode != 0:
        sys.exit(result.returncode)

    for module in BATCH_DEPENDENCY_IMPORTS:
        try:
            __import__(module)
        except ImportError:
            print('Batch flash dependency is not importable after setup: %s' % module,
                  file=sys.stderr)
            sys.exit(1)


def _run_batch_tool(args, device, pid, xlsx, sheet, list_ports, dry_run,
                    jobs, timeout, yes, retry_uuid) -> None:
    project_dir = os.path.abspath(args.project_dir)
    build_dir = os.path.abspath(args.build_dir)
    cmd = [
        sys.executable, BATCH_TOOL_PATH,
        '--project-dir', project_dir,
        '--build-dir', build_dir,
        '--baud', str(args.baud),
    ]
    if args.port:
        cmd += ['--port', args.port]
    if pid is not None:
        cmd += ['--pid', pid]
    for path in device:
        cmd += ['--device', path]
    for option, value in (
            ('xlsx', xlsx),
            ('sheet', sheet),
            ('jobs', jobs),
            ('timeout', timeout),
            ('retry-uuid', retry_uuid)):
        if value is not None:
            cmd += ['--' + option, str(value)]
    for option, enabled in (
            ('list-ports', list_ports),
            ('dry-run', dry_run),
            ('yes', yes)):
        if enabled:
            cmd += ['--' + option]

    process = None
    interrupted = None

    def cancel_backend(signum, frame):
        nonlocal interrupted
        interrupted = signum
        if process is not None:
            process.send_signal(signum)

    previous_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, cancel_backend)
        # Keep stdin/foreground-group membership for the confirmation prompt.
        # subprocess.run kills on KeyboardInterrupt, before detached tools and
        # workbook outcomes can be cleaned up by the backend.
        process = subprocess.Popen(cmd)
        if interrupted is not None:
            process.send_signal(interrupted)
        returncode = process.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if interrupted is not None:
        sys.exit(130)
    if returncode != 0:
        sys.exit(returncode)


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

    def tuya_batch_setup(name, ctx, args) -> None:
        _run_batch_setup()

    def tuya_batch_flash(name, ctx, args, device=(), pid=None, xlsx=None,
                         sheet=None, list_ports=False, dry_run=False, jobs=None,
                         timeout=None, yes=False, retry_uuid=None) -> None:
        _run_batch_tool(args, device, pid, xlsx, sheet, list_ports, dry_run,
                        jobs, timeout, yes, retry_uuid)

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
            'tuya-batch-setup': {
                'callback': tuya_batch_setup,
                'short_help': 'Install and verify batch flash Python dependencies.',
                'dependencies': [],
                'options': [],
            },
            'tuya-batch-flash': {
                'callback': tuya_batch_flash,
                'short_help': 'Flash firmware and unique Tuya credentials to a device batch.',
                'dependencies': [],
                'options': [
                    {
                        'names': ['--pid'],
                        'help': 'Tuya product key (required to flash, dry-run, or retry).',
                    },
                    {
                        'names': ['--device'],
                        'multiple': True,
                        'metavar': 'PORT',
                        'help': 'Restrict to this port path (repeatable).',
                    },
                    {
                        'names': ['--xlsx'],
                        'help': 'Credential workbook (default: <project>/auth-info.xlsx).',
                    },
                    {
                        'names': ['--sheet'],
                        'help': 'Worksheet containing uuid/key headers.',
                    },
                    {
                        'names': ['--list-ports'],
                        'is_flag': True,
                        'help': 'List USB serial candidates and exit.',
                    },
                    {
                        'names': ['--dry-run'],
                        'is_flag': True,
                        'help': 'Validate and preview without opening devices or writing.',
                    },
                    {
                        'names': ['--jobs'],
                        'type': int,
                        'help': 'Maximum concurrent device jobs.',
                    },
                    {
                        'names': ['--timeout'],
                        'type': float,
                        'help': 'Positive per-command timeout in seconds.',
                    },
                    {
                        'names': ['--yes'],
                        'is_flag': True,
                        'help': 'Authorize the displayed candidate set without prompting.',
                    },
                    {
                        'names': ['--retry-uuid'],
                        'metavar': 'UUID',
                        'help': 'Retry one failed/interrupted row with one --device.',
                    },
                ],
            },
        },
    }
