#!/usr/bin/env python3
# Tuya auth credential tool: write/read the device identity (uuid, auth_key,
# product_key) on the device's nvs partition.
#
# write: parse tuya_authkey.txt, generate an NVS partition image with IDF's
#        nvs_partition_gen, flash it with parttool, read back and verify.
# read:  dump the partition with parttool and print the stored identity.
#
# WARNING: flashing replaces the whole nvs partition. WiFi credentials, device
# settings and the Tuya activation state are erased; the device must re-pair
# with the Tuya app afterwards.
#
# Run inside the ESP-IDF Python environment (idf.py does this automatically).

import argparse
import csv
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

NAMESPACE = "tuya_auth"
KEYS = ("uuid", "auth_key", "product_key")
ENV_KEY_MAP = {
    "uuid": "TUYA_UUID",
    "auth_key": "TUYA_AUTH_KEY",
    "product_key": "TUYA_PRODUCT_KEY",
}
# tuya_authkey.txt KEY -> logical key
FILE_KEY_MAP = {v: k for k, v in ENV_KEY_MAP.items()}

# Realistic length limits, mirrored by the firmware loader in main/tuya_auth.cc.
# Minimums come from the BLE SDK's fixed-length reads (16/32/16 bytes).
LENGTH_LIMITS = {
    "uuid": (16, 31),
    "auth_key": (32, 63),
    "product_key": (16, 31),
}


class AuthToolError(RuntimeError):
    """Secret-free error raised by reusable auth operations."""

    def __init__(self, stage, message, detail=None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.detail = detail

    def __str__(self):
        if self.detail:
            return "%s: %s" % (self.message, self.detail)
        return self.message


class CommandCancelled(AuthToolError):
    def __init__(self, stage="cancelled"):
        super().__init__(stage, "operation cancelled")


def check_cancelled(cancel_event, stage):
    """Raise CommandCancelled if the shared cancellation event is set."""
    if cancel_event is not None and cancel_event.is_set():
        raise CommandCancelled(stage)


def _terminate_process(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
        if os.name != "posix":
            return
    except subprocess.TimeoutExpired:
        pass
    # A reaped leader does not imply its esptool descendants have exited.
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command, stage, timeout=None, cancel_event=None, env=None):
    """Run a command with captured output and process-group cancellation."""
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": env,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    check_cancelled(cancel_event, stage)
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise AuthToolError(stage, "could not start command", str(exc)) from exc

    try:
        started = time.monotonic()
        while True:
            check_cancelled(cancel_event, stage)
            remaining = None
            if timeout is not None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise AuthToolError(stage, "command timed out after %.1fs" % timeout)
            try:
                stdout, stderr = process.communicate(
                    timeout=min(0.2, remaining) if remaining is not None else 0.2)
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()

    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode != 0:
        lines = [line.strip() for line in (stdout or "").splitlines()
                 if "fatal error" in line.lower()]
        fallback = (stderr or stdout or "").strip().splitlines()[-5:]
        detail = " | ".join(lines or fallback) or "command failed"
        raise AuthToolError(stage, "command failed", detail)
    return result


def find_idf_tools_structured():
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        raise AuthToolError(
            "preflight", "IDF_PATH is not set",
            "run from an ESP-IDF environment (or via idf.py)")
    parttool = os.path.join(idf_path, "components", "partition_table", "parttool.py")
    if not os.path.isfile(parttool):
        raise AuthToolError("preflight", "parttool.py not found", parttool)
    return parttool


def find_idf_tools():
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        sys.exit("error: IDF_PATH is not set; run this from an ESP-IDF environment "
                 "(or via: idf.py tuya-auth-flash)")
    parttool = os.path.join(idf_path, "components", "partition_table", "parttool.py")
    if not os.path.isfile(parttool):
        sys.exit("error: parttool.py not found under IDF_PATH: %s" % parttool)
    return parttool


def parse_authkey_file(path):
    """Parse the KEY=VALUE tuya_authkey.txt format into a dict."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                sys.exit("error: %s:%d: expected KEY=VALUE, got %r" % (path, line_no, line))
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in FILE_KEY_MAP:
                sys.exit("error: %s:%d: unexpected key %r (expected %s)"
                         % (path, line_no, key,
                            ", ".join(sorted(FILE_KEY_MAP))))
            values[FILE_KEY_MAP[key]] = value.strip()
    return values


def validate(values, source):
    for key in KEYS:
        if key not in source:
            sys.exit("error: %s: missing %s" % (source, ENV_KEY_MAP[key]))
        value = source[key]
        if not value:
            sys.exit("error: %s: %s is empty" % (source, ENV_KEY_MAP[key]))
    for key in KEYS:
        value = values.get(key, "")
        if value:
            lo, hi = LENGTH_LIMITS[key]
            if not (lo <= len(value) <= hi):
                sys.exit("error: %s length %d out of range %d..%d"
                         % (ENV_KEY_MAP[key], len(value), lo, hi))


def apply_overrides(values, args):
    source = dict(values)
    for key, flag in (("uuid", args.uuid), ("auth_key", args.auth_key),
                      ("product_key", args.pid)):
        if flag is not None:
            values[key] = flag
            source[key] = flag
    validate(values, source)


def values_to_nvs_csv(values):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["key", "type", "encoding", "value"])
    writer.writerow([NAMESPACE, "namespace", "", ""])
    for key in KEYS:
        writer.writerow([key, "data", "string", values[key]])
    return buf.getvalue()


def _fail_parttool(action, detail_lines):
    lines = [l.strip() for l in detail_lines if l.strip()]
    if not lines:
        lines = ["is the port correct and is the serial monitor closed?"]
    sys.exit("error: parttool %s failed:\n  %s\nHint: close the serial "
             "monitor and check -p/--port." % (action, "\n  ".join(lines)))


def run_parttool_operation(parttool, port, baud, extra, stage,
                           timeout=None, cancel_event=None, *, before=None, after=None,
                           partition_table_offset=None):
    cmd = [sys.executable, parttool]
    reset_args = ["%s=%s" % (name, value) for name, value in
                  (("before", before), ("after", after)) if value is not None]
    if reset_args:
        cmd += ["--esptool-args"] + reset_args
    # --port terminates parttool's greedy --esptool-args list before the action.
    cmd += ["--port", port, "--quiet"]
    if baud:
        cmd += ["--baud", str(baud)]
    if partition_table_offset is not None:
        cmd += ["--partition-table-offset", hex(partition_table_offset)]
    cmd += extra
    return run_command(cmd, stage, timeout=timeout, cancel_event=cancel_event)


def run_parttool(parttool, port, baud, extra, capture=True):
    """Run parttool.py while preserving the original CLI error contract."""
    try:
        result = run_parttool_operation(
            parttool, port, baud, extra, "parttool_%s" % extra[0])
    except AuthToolError as exc:
        _fail_parttool(extra[0], [exc.detail or exc.message])
    if not capture and result.stdout:
        print(result.stdout, end="")
    return result


def get_partition_size_operation(parttool, port, baud, timeout=None,
                                 cancel_event=None, *, before=None, after=None,
                                 partition_table_offset=None):
    res = run_parttool_operation(
        parttool, port, baud,
        ["get_partition_info", "--partition-name", "nvs", "--info", "size"],
        "auth_write", timeout, cancel_event, before=before, after=after,
        partition_table_offset=partition_table_offset)
    lines = res.stdout.strip().splitlines()
    size_text = lines[-1].strip() if lines else ""
    try:
        size = int(size_text, 0)
    except ValueError as exc:
        raise AuthToolError("auth_write", "could not parse nvs partition size") from exc
    if size <= 0:
        raise AuthToolError("auth_write", "invalid nvs partition size")
    return size


def get_partition_size(parttool, port, baud):
    res = run_parttool(parttool, port, baud,
                       ["get_partition_info", "--partition-name", "nvs",
                        "--info", "size"])
    size = res.stdout.strip().splitlines()[-1].strip()
    try:
        return int(size, 0)
    except ValueError:
        sys.exit("error: could not parse nvs partition size from: %r" % size)


def parse_nvs_dump(image_path, strict=False):
    """Parse an NVS partition image, return {namespace: {key: value}} for strings."""
    idf_path = os.environ["IDF_PATH"]
    tool_dir = os.path.join(idf_path, "components", "nvs_flash", "nvs_partition_tool")
    sys.path.insert(0, tool_dir)
    from nvs_parser import NVS_Partition  # noqa: E402

    with open(image_path, "rb") as f:
        partition = NVS_Partition("nvs", bytearray(f.read()))

    namespaces = {}
    for page in partition.pages:
        for entry in page.entries:
            if entry.state != "Written":
                continue
            meta = entry.metadata
            if meta["namespace"] == 0:
                namespaces[entry.data["value"]] = entry.key
                continue
            if meta["type"] != "string":
                continue
            raw = b"".join(bytes(child.raw) for child in entry.children)
            size = entry.data["size"]
            if strict:
                if size < 1 or size > len(raw) or raw[size - 1] != 0:
                    raise ValueError("malformed NVS string payload")
                value = raw[:size - 1].decode("utf-8")
            else:
                value = raw[:size].rstrip(b"\x00").decode("utf-8", errors="replace")
            ns_name = namespaces.get(meta["namespace"], "?")
            namespaces.setdefault(ns_name, {})[entry.key] = value
    return {name: data for name, data in namespaces.items()
            if name != "?" or isinstance(data, dict) and data}


def validate_values_structured(values):
    for key in KEYS:
        value = values.get(key)
        if not isinstance(value, str) or not value:
            raise AuthToolError("auth_write", "missing credential field %s" % key)
        if "\x00" in value:
            raise AuthToolError("auth_write", "credential field %s contains NUL" % key)
        lo, hi = LENGTH_LIMITS[key]
        byte_length = len(value.encode("utf-8"))
        if not lo <= byte_length <= hi:
            raise AuthToolError(
                "auth_write", "%s UTF-8 length %d out of range %d..%d"
                % (key, byte_length, lo, hi))


def write_verify_identity(port, baud, values, timeout=None, cancel_event=None,
                          stage_callback=None, temp_parent=None, *,
                          before=None, after=None, partition_table_offset=None):
    """Write/verify silently; optional resets apply to EVERY nested esptool call.

    Batch callers should use after='no_reset' and apply the manifest's final
    reset separately after verification. partition_table_offset is an integer
    byte offset used for every parttool call. None preserves parttool defaults.
    """
    validate_values_structured(values)
    parttool = find_idf_tools_structured()
    check_cancelled(cancel_event, "auth_write")
    if stage_callback:
        stage_callback("auth_write")
    size = get_partition_size_operation(
        parttool, port, baud, timeout=timeout, cancel_event=cancel_event,
        before=before, after=after, partition_table_offset=partition_table_offset)
    check_cancelled(cancel_event, "auth_write")

    with tempfile.TemporaryDirectory(prefix="tuya_auth_", dir=temp_parent) as tmp:
        os.chmod(tmp, 0o700)
        csv_path = os.path.join(tmp, "tuya_auth.csv")
        image_path = os.path.join(tmp, "tuya_auth.bin")
        dump_path = os.path.join(tmp, "nvs_dump.bin")
        fd = os.open(csv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as csv_file:
            csv_file.write(values_to_nvs_csv(values))

        run_command(
            [sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate",
             csv_path, image_path, hex(size)],
            "auth_write", timeout=timeout, cancel_event=cancel_event)
        check_cancelled(cancel_event, "auth_write")
        os.chmod(image_path, 0o600)
        run_parttool_operation(
            parttool, port, baud,
            ["write_partition", "--partition-name", "nvs", "--input", image_path],
            "auth_write", timeout, cancel_event, before=before, after=after,
            partition_table_offset=partition_table_offset)

        if stage_callback:
            stage_callback("auth_read")
        check_cancelled(cancel_event, "auth_read")
        run_parttool_operation(
            parttool, port, baud,
            ["read_partition", "--partition-name", "nvs", "--output", dump_path],
            "auth_read", timeout, cancel_event, before=before, after=after,
            partition_table_offset=partition_table_offset)
        if stage_callback:
            stage_callback("auth_verify")
        check_cancelled(cancel_event, "auth_verify")
        try:
            with open(image_path, "rb") as image, open(dump_path, "rb") as dump:
                if dump.read() != image.read():
                    raise ValueError("nvs read-back differs from generated image")
            stored = parse_nvs_dump(dump_path, strict=True).get(NAMESPACE, {})
        except Exception as exc:
            raise AuthToolError("auth_verify", "malformed nvs read-back") from exc
        mismatch = [key for key in KEYS if stored.get(key) != values[key]]
        if mismatch:
            raise AuthToolError(
                "auth_verify", "verification failed for fields: %s"
                % ", ".join(mismatch))
    return True


def cmd_write(args):
    parttool = find_idf_tools()
    file_values = parse_authkey_file(args.file) if os.path.exists(args.file) else {}
    values = dict(file_values)
    apply_overrides(values, args)

    print("Writing identity to nvs partition (this erases WiFi/settings/activation):")
    for key in KEYS:
        print("  %s = %s" % (key, values[key]))

    size = get_partition_size(parttool, args.port, args.baud)
    print("nvs partition size: %#x bytes" % size)

    with tempfile.TemporaryDirectory(prefix="tuya_auth_") as tmp:
        csv_path = os.path.join(tmp, "tuya_auth.csv")
        image_path = os.path.join(tmp, "tuya_auth.bin")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(values_to_nvs_csv(values))

        gen = subprocess.run(
            [sys.executable, "-m", "esp_idf_nvs_partition_gen",
             "generate", csv_path, image_path, hex(size)],
            capture_output=True, text=True)
        if gen.returncode != 0:
            sys.exit("error: nvs image generation failed:\n%s"
                     % (gen.stderr or gen.stdout).strip())

        run_parttool(parttool, args.port, args.baud,
                     ["write_partition", "--partition-name", "nvs",
                      "--input", image_path], capture=False)
        print("nvs partition written.")

        if args.no_verify:
            print("Read-back verification skipped (--no-verify).")
            return

        dump_path = os.path.join(tmp, "nvs_dump.bin")
        run_parttool(parttool, args.port, args.baud,
                     ["read_partition", "--partition-name", "nvs",
                      "--output", dump_path])
        stored = parse_nvs_dump(dump_path).get(NAMESPACE, {})
        mismatch = [k for k in KEYS if stored.get(k) != values[k]]
        if mismatch:
            sys.exit("error: verification FAILED for: %s" % ", ".join(mismatch))
        print("Read-back verification passed.")


def cmd_read(args):
    parttool = find_idf_tools()
    with tempfile.TemporaryDirectory(prefix="tuya_auth_") as tmp:
        dump_path = os.path.join(tmp, "nvs_dump.bin")
        run_parttool(parttool, args.port, args.baud,
                     ["read_partition", "--partition-name", "nvs",
                      "--output", dump_path])
        stored = parse_nvs_dump(dump_path)
    ns = stored.get(NAMESPACE, {})
    if not ns:
        sys.exit("error: no %s namespace found on the device "
                 "(flash it with: idf.py tuya-auth-flash)" % NAMESPACE)
    for key in KEYS:
        if key in ns:
            print("%s=%s" % (ENV_KEY_MAP[key], ns[key]))
        else:
            print("# %s: not set" % key)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--port", default=os.environ.get("ESPPORT"),
                        help="serial port (default: $ESPPORT)")
    common.add_argument("--baud", type=int, default=None,
                        help="parttool/esptool baud rate")

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p_write = sub.add_parser("write", parents=[common],
                             help="write tuya_authkey.txt to the nvs partition")
    p_write.add_argument("--file", default=None,
                         help="credential file (default: <project>/tuya_authkey.txt)")
    p_write.add_argument("--uuid", default=None, help="override uuid")
    p_write.add_argument("--auth-key", dest="auth_key", default=None,
                         help="override auth_key")
    p_write.add_argument("--pid", default=None, help="override product_key")
    p_write.add_argument("--no-verify", action="store_true",
                         help="skip read-back verification")
    p_write.set_defaults(func=cmd_write)

    p_read = sub.add_parser("read", parents=[common],
                            help="print the identity stored on the device")
    p_read.set_defaults(func=cmd_read)

    args = parser.parse_args()
    if not args.port:
        parser.error("-p/--port is required (or set $ESPPORT)")

    # Project root = parent of the scripts/ directory holding this file.
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if getattr(args, "file", None) is None:
        args.file = os.path.join(project_dir, "tuya_authkey.txt")

    args.func(args)


if __name__ == "__main__":
    main()
