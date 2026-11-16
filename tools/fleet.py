#!/usr/bin/env python3
"""Run an explicit maintenance command on named Pi hosts over verified SSH.

Uses SSH keys by default. An optional --password-file is passed to sshpass;
passwords are never included in command arguments or output. New host keys use
OpenSSH's accept-new policy; changed keys are rejected.
"""
import argparse
import concurrent.futures
import json
import subprocess


def run(host, command, password_file=None, timeout=60):
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "NumberOfPasswordPrompts=1"]
    args += ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/immersive-ssh-%C",
             "-o", "ControlPersist=600"]
    if password_file:
        args += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password"]
        args = ["sshpass", "-f", password_file] + args
    else:
        args += ["-o", "BatchMode=yes"]
    try:
        result = subprocess.run(args + [f"root@{host}", command],
                                capture_output=True, text=True, timeout=timeout)
        return {"host": host, "code": result.returncode, "output": result.stdout,
                "error": result.stderr}
    except subprocess.TimeoutExpired:
        return {"host": host, "code": 124, "error": "SSH command timed out"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-file")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--hosts", nargs="+", required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(lambda host: run(host, args.command, args.password_file, args.timeout), args.hosts)
        failed = False
        for result in results:
            print(json.dumps(result), flush=True)
            failed |= result["code"] != 0
    raise SystemExit(int(failed))


if __name__ == "__main__":
    main()
