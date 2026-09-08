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
import subprocess
import sys
import tempfile

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


def run_parttool(parttool, port, baud, extra, capture=True):
    """Run parttool.py, always capturing output.

    Capture is unconditional because parttool's --quiet cannot suppress
    errors raised before its own try/except (e.g. partition-table read
    failures in ParttoolTarget.__init__), which otherwise leak tracebacks.
    Success output is echoed afterwards so progress stays visible.
    """
    cmd = [sys.executable, parttool, "--port", port, "--quiet"]
    if baud:
        cmd += ["--baud", str(baud)]
    cmd += extra
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=True)
    except subprocess.CalledProcessError as exc:
        # esptool's friendly diagnostics go to parttool's stdout; the
        # stderr traceback tail is only a fallback.
        out_lines = [l for l in (exc.stdout or "").splitlines()
                     if "fatal error" in l.lower()]
        detail = out_lines or (exc.stderr or "").strip().splitlines()[-5:]
        _fail_parttool(extra[0], detail)
    if not capture and result.stdout:
        print(result.stdout, end="")
    return result


def get_partition_size(parttool, port, baud):
    res = run_parttool(parttool, port, baud,
                       ["get_partition_info", "--partition-name", "nvs",
                        "--info", "size"])
    size = res.stdout.strip().splitlines()[-1].strip()
    try:
        return int(size, 0)
    except ValueError:
        sys.exit("error: could not parse nvs partition size from: %r" % size)


def parse_nvs_dump(image_path):
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
            # Trailing NULs are cell padding, not part of the stored value.
            value = raw[: entry.data["size"]].rstrip(b"\x00").decode(
                "utf-8", errors="replace")
            ns_name = namespaces.get(meta["namespace"], "?")
            namespaces.setdefault(ns_name, {})[entry.key] = value
    return {name: data for name, data in namespaces.items()
            if name != "?" or isinstance(data, dict) and data}


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
