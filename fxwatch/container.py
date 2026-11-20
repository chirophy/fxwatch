"""Single-container startup and child lifecycle management (Linux production)."""
from pathlib import Path
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from .config import load
from .engine import connect

log = logging.getLogger('fxwatch.container')


def prepare_config(destination, template):
    """Seed a new data volume, preserving any configuration already present."""
    destination = Path(destination)
    if destination.exists():
        if not destination.is_file():
            raise ValueError('FXWATCH_CONFIG must point to a configuration file')
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Path(template).open('rb') as source:
        try:
            with destination.open('xb') as target:
                shutil.copyfileobj(source, target)
        except FileExistsError:
            pass
    return destination


def child_commands(config):
    collector = [sys.executable, '-m', 'fxwatch', '--config', str(config),
                 'run', '--report', str(load(config)['database'].parent / 'report.html')]
    web = [sys.executable, '-m', 'gunicorn', '--workers', '2', '--threads', '2',
           '--bind', '0.0.0.0:8099', '--worker-tmp-dir', '/tmp',
           '--access-logfile', '-', '--error-logfile', '-', 'fxwatch.web:create_app()']
    environment = dict(os.environ, FXWATCH_CONFIG=str(config))
    web_environment = {key: value for key, value in environment.items()
                       if key not in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')}
    return [('collector', collector, environment), ('web', web, web_environment)]


def signal_child(process, force=False):
    if os.name == 'posix':
        # Include Gunicorn workers, even if their master has already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:  # Local Windows tests; production is Linux.
        process.kill() if force else process.terminate()


def supervise(commands, stop, shutdown_timeout=70):
    processes = []
    try:
        for name, command, environment in commands:
            if stop.is_set():
                return 0
            process = subprocess.Popen(command, env=environment, start_new_session=(os.name == 'posix'))
            processes.append((name, process))
            log.info('%s started', name)
        while not stop.wait(0.2):
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    log.error('%s exited unexpectedly (%s); stopping container', name, code)
                    return 1
        return 0
    finally:
        for _name, process in processes:
            signal_child(process)
        deadline = time.monotonic() + shutdown_timeout
        for _name, process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        # Clean remaining descendants as well as any unresponsive child master.
        for _name, process in processes:
            signal_child(process, force=True)
            process.wait()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stop = threading.Event()
    previous_handlers = {sig: signal.signal(sig, lambda *_: stop.set())
                         for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        config = prepare_config(os.environ.get('FXWATCH_CONFIG', '/data/config.toml'),
                                '/etc/fxwatch/default.toml')
        settings = load(config)
        db = connect(settings['database'])
        db.close()
        log.info('Database ready; starting collector and web on port 8099')
        return supervise(child_commands(config), stop)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        log.error('Container startup failed: %s', exc)
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
