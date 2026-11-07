#!/usr/bin/env python3
"""Windows control script for the dsh-superagent service.

Mirrors scripts/dsh-superagent (start/stop/restart/status) for Windows:
tmux on Windows (tmux-windows) provides the session; process detection uses
wmic, and file locks use msvcrt. Exit codes match the Linux script:
0 running normally, 1 stopped/duplicated/unreachable/runtime error,
2 component error, 3 waiting for setup.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import msvcrt
except ImportError:
    msvcrt = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = os.path.join(ROOT, '.artifacts')
CONTROL = os.path.join(ARTIFACTS, 'control')
KEEP_LOGS = 20
PART_ERROR = 2
SETUP_WAITS = 3


def control_dir(profile):
    directory = os.path.join(CONTROL, 'profiles', profile)
    os.makedirs(directory, exist_ok=True)
    return directory


def stopped(profile):
    return os.path.exists(os.path.join(control_dir(profile), 'manually-stopped'))


def _ps_processes():
    """Return [(pid, name, cmdline)] of all processes via PowerShell."""
    cmd = ("powershell -NoProfile -Command \""
           "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
           "Get-CimInstance Win32_Process | ForEach-Object { "
           "'{0}|{1}|{2}' -f $_.ProcessId, $_.Name, $_.CommandLine }\"")
    result = subprocess.run(cmd, capture_output=True, shell=True,
                            encoding='utf-8', errors='replace')
    entries = []
    for line in (result.stdout or '').splitlines():
        parts = line.split('|', 2)
        if len(parts) == 3 and parts[0].isdigit():
            entries.append((int(parts[0]), parts[1], parts[2] or ''))
    return entries


def _is_service(name, cmdline, profile):
    low = name.lower()
    if not any(part in low for part in ('node', 'dsh', 'deepseek harness')):
        return False
    if 'plugin' in cmdline:
        return False
    return f'--profile {profile}' in cmdline


def running_windows(pid, profile):
    """True if `pid` currently runs the DSH service for `profile`."""
    for found, name, cmdline in _ps_processes():
        if found == pid and _is_service(name, cmdline, profile):
            return True
    return False


def processes(profile):
    """PIDs of DSH service processes for `profile` (PowerShell-based, Windows)."""
    pids = []
    for pid, name, cmdline in _ps_processes():
        if _is_service(name, cmdline, profile):
            pids.append(pid)
    try:
        recorded = int(open(os.path.join(control_dir(profile), 'service.pid')).read())
        if recorded not in pids and running_windows(recorded, profile):
            pids.append(recorded)
    except (OSError, ValueError):
        pass
    return pids


def state(url):
    """Fetch health/setup from the management API, or None if unreachable."""
    request = urllib.request.Request(url.rstrip('/') + '/orchestrator/api/state',
                                     headers={'x-orchestrator-request': '1'})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            value = json.load(response)
        health, setup = value.get('health') or {}, value.get('setup') or {}
        entries = {name: entry if isinstance(entry, dict) else {} for name, entry in health.items()}
        return {'health': {name: entry.get('state') for name, entry in entries.items()},
                'errors': {name: str(entry.get('detail', '')) for name, entry in entries.items()
                            if entry.get('state') == 'error'},
                'setup': setup.get('problem'), 'feishu': setup.get('feishuProblem')}
    except (OSError, ValueError, AttributeError):
        return None


def runtime_up(current):
    return current is not None and current['health'].get('runtime') in ('ready', 'paused')


def part_errors(current):
    return {name: detail for name, detail in sorted(current['errors'].items()) if name != 'runtime'}


def outcome(pids, current):
    if len(pids) != 1 or not runtime_up(current):
        return 1
    if current['setup']:
        return SETUP_WAITS
    return PART_ERROR if part_errors(current) else 0


def status(args):
    pids = processes(args.profile)
    current = state(args.url) if pids else None
    print(json.dumps({'pids': pids, 'cwd': ROOT, 'url': args.url,
                      'manually_stopped': stopped(args.profile),
                      'health': current and current['health'],
                      'errors': current and current['errors'],
                      'setup': current and current['setup'],
                      'feishu': current and current['feishu']}, ensure_ascii=False, indent=2))
    return outcome(pids, current)


def stop(args, intentional=True):
    pids = processes(args.profile)
    if len(pids) > 1:
        raise RuntimeError('Multiple dsh-superagent processes found; refusing to choose one.')
    if intentional:
        with open(os.path.join(control_dir(args.profile), 'manually-stopped'), 'w') as f:
            f.write(time.strftime('%Y-%m-%dT%H:%M:%S%z') + '\n')
    tmux = _find_tmux()
    if tmux:
        # Kill the whole tmux session (cmd pane + its node child tree).
        subprocess.run([tmux, 'kill-session', '-t', '=dsh-' + args.profile],
                       capture_output=True)
    time.sleep(1)
    for pid in pids:
        if not running_windows(pid, args.profile):
            continue
        subprocess.run(['taskkill', '/PID', str(pid), '/T'], capture_output=True)
    deadline = time.monotonic() + args.stop_timeout
    while any(running_windows(pid, args.profile) for pid in pids):
        if time.monotonic() >= deadline:
            raise RuntimeError('Graceful stop timed out; check status.')
        time.sleep(0.5)
    pid_file = os.path.join(control_dir(args.profile), 'service.pid')
    if os.path.exists(pid_file):
        os.remove(pid_file)
    print('dsh-superagent stopped.' if not intentional
          else 'dsh-superagent stopped. Automatic recovery is paused until start/restart.')


def prune_logs(profile):
    name_re = re.compile(re.escape(f'dsh-superagent-{profile}-') + r'\d{8}-\d{6}-\d+\.log')
    logs = sorted((os.path.join(ARTIFACTS, p) for p in os.listdir(ARTIFACTS)
                   if name_re.fullmatch(p)),
                  key=os.path.getmtime)
    for path in logs[:-KEEP_LOGS]:
        try:
            os.remove(path)
        except OSError:
            pass


def _find_tmux():
    found = shutil.which('tmux')
    if found:
        return found
    base = os.path.expandvars(
        r'%LOCALAPPDATA%\Microsoft\WinGet\Packages')
    if os.path.isdir(base):
        for entry in os.listdir(base):
            if 'tmux' in entry.lower():
                candidate = os.path.join(base, entry, 'tmux.exe')
                if os.path.isfile(candidate):
                    return candidate
    return None


def start(args):
    session = 'dsh-' + args.profile
    pids = processes(args.profile)
    if len(pids) > 1:
        raise RuntimeError('Multiple dsh-superagent processes found; refusing to start another.')
    if not pids:
        dsh = shutil.which('dsh')
        tmux = _find_tmux()
        if not dsh:
            raise RuntimeError('dsh must be on PATH.')
        exists = tmux is not None and subprocess.run(
            [tmux, 'has-session', '-t', '=' + session],
            capture_output=True).returncode == 0
        if state(args.url) is not None:
            raise RuntimeError('An existing orchestrator answers at this URL; refusing duplicate startup.')
        stamp = time.strftime('%Y%m%d-%H%M%S')
        log = os.path.join(ARTIFACTS, f'dsh-superagent-{args.profile}-{stamp}-{os.getpid()}.log')
        open(log, 'a').close()
        prune_logs(args.profile)
        if tmux:
            # tmux-windows does not support -c; set cwd inside cmd instead.
            command = (f'cmd /c "cd /d {ROOT} && "{dsh}" --profile {args.profile} '
                       f'--no-open >> "{log}" 2>&1"')
            target = [tmux, 'new-window', '-t', '=' + session + ':'] if exists \
                else [tmux, 'new-session', '-s', session]
            target += ['-d', '-P', '-F', '#{pane_pid}', '-n', 'server', command]
            proc = subprocess.run(target, check=True, capture_output=True,
                                  encoding='utf-8', errors='replace')
            pane = proc.stdout.strip()
            if pane.isdigit():
                with open(os.path.join(control_dir(args.profile), 'service.pid'), 'w') as f:
                    f.write(pane + '\n')
        else:
            # No tmux on this host: a detached process keeps the service alive
            # after this console exits, and cmd still resolves the dsh.cmd shim.
            detached = (subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP)
            with open(log, 'ab') as out:
                proc = subprocess.Popen(
                    [dsh, '--profile', args.profile, '--no-open'],
                    cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, creationflags=detached,
                    close_fds=True)
            with open(os.path.join(control_dir(args.profile), 'service.pid'), 'w') as f:
                f.write(f'{proc.pid}\n')
            print('tmux is not installed; started a detached process instead.',
                  flush=True)
        print('Log: ' + log, flush=True)
    else:
        with open(os.path.join(control_dir(args.profile), 'service.pid'), 'w') as f:
            f.write(f'{pids[0]}\n')
        print(f'dsh-superagent already running (PID {pids[0]}); no duplicate started.', flush=True)
    pid_file = os.path.join(control_dir(args.profile), 'manually-stopped')
    if os.path.exists(pid_file):
        os.remove(pid_file)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        current = state(args.url)
        if processes(args.profile) and runtime_up(current):
            code = status(args)
            if current['setup']:
                print('dsh-superagent is waiting for setup: ' + str(current['setup'].get('detail', '')))
                print('Open the URL DSH printed in the log, then the dsh-superagent overview.')
            if current['feishu']:
                print('dsh-superagent runs without Feishu: ' + str(current['feishu'].get('detail', '')))
            for name, detail in part_errors(current).items():
                print(f'{name} reports an error, which a restart does not fix: {detail}')
            return code
        time.sleep(1)
    status(args)
    raise RuntimeError('Startup health check timed out; inspect the log.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status'])
    parser.add_argument('--profile', default=os.environ.get('DSH_SUPERAGENT_PROFILE') or 'superagent')
    parser.add_argument('--url', default=os.environ.get('DSH_SUPERAGENT_URL') or 'http://127.0.0.1:3082')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--stop-timeout', type=float, default=300)
    parser.add_argument('--respect-stop', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0 or args.stop_timeout <= 0:
        parser.error('--timeout and --stop-timeout must be positive')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', args.profile):
        parser.error('--profile must be a DSH profile name')

    lock_path = os.path.join(control_dir(args.profile), 'operation.lock')
    lock_file = open(lock_path, 'a+b')
    lock_file.write(b'\0')
    lock_file.flush()
    lock_file.seek(0)
    if msvcrt:
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Another dsh-superagent control operation is in progress.')
    else:
        try:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except OSError:
            raise RuntimeError('Another dsh-superagent control operation is in progress.')

    try:
        if args.respect_stop and args.action in ('start', 'restart') and stopped(args.profile):
            print('Intentional stop is active; automatic recovery skipped.')
            return 0
        if args.action == 'status':
            return status(args)
        if args.action in ('stop', 'restart'):
            stop(args, intentional=(args.action == 'stop'))
        if args.action in ('start', 'restart'):
            return start(args)
    finally:
        if msvcrt:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        lock_file.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
