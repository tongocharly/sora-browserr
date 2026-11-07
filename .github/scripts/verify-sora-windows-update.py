#!/usr/bin/env python3
"""Exercise Sora's automatic signed updater in a disposable Windows CI profile."""
import argparse
import base64
import configparser
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET


class Marionette:
    def __init__(self, port):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=10)
        self.socket.settimeout(40)
        self.sequence = 0
        hello = self.receive()
        if hello.get('marionetteProtocol') != 3:
            raise RuntimeError('Unexpected Gecko automation protocol')
        session = self.command('WebDriver:NewSession', {'capabilities': {'alwaysMatch': {'acceptInsecureCerts': False}}})
        self.pid = session.get('capabilities', {}).get('moz:processID')
        self.command('Marionette:SetContext', {'value': 'chrome'})

    def receive(self):
        length = b''
        while True:
            byte = self.socket.recv(1)
            if not byte:
                raise EOFError('Gecko connection closed')
            if byte == b':':
                break
            length += byte
            if len(length) > 12 or not byte.isdigit():
                raise RuntimeError('Invalid protocol frame')
        size = int(length)
        if size > 32 * 1024 * 1024:
            raise RuntimeError('Protocol frame too large')
        data = bytearray()
        while len(data) < size:
            part = self.socket.recv(size - len(data))
            if not part:
                raise EOFError('Incomplete protocol frame')
            data.extend(part)
        return json.loads(data)

    def command(self, name, params):
        self.sequence += 1
        payload = json.dumps([0, self.sequence, name, params]).encode()
        self.socket.sendall(str(len(payload)).encode() + b':' + payload)
        reply = self.receive()
        if reply[0] != 1 or reply[1] != self.sequence:
            raise RuntimeError('Out-of-order Gecko response')
        if reply[2]:
            raise RuntimeError(json.dumps(reply[2]))
        result = reply[3]
        return result.get('value', result) if isinstance(result, dict) else result

    def script(self, source, asynchronous=False):
        return self.command('WebDriver:ExecuteAsyncScript' if asynchronous else 'WebDriver:ExecuteScript', {
            'script': source, 'args': [], 'newSandbox': True, 'sandbox': 'default',
            'line': 1, 'filename': 'Sora Windows isolated update verification',
        })

    def close(self):
        self.socket.close()


def connect(port, seconds=90):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            return Marionette(port)
        except (OSError, EOFError, RuntimeError) as error:
            last = error
            time.sleep(1)
    raise RuntimeError('Sora automation did not become ready: ' + str(last))


def identity(client):
    return client.script('''
      return {name: Services.appinfo.name, version: Services.appinfo.version,
        buildID: Services.appinfo.appBuildID, platformVersion: Services.appinfo.platformVersion,
        os: Services.appinfo.OS, locales: Services.locale.availableLocales};
    ''')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--mar', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This integration check must run on Windows')
    target = configparser.ConfigParser()
    target.read(args.target / 'application.ini')
    version = target['App']['Version']
    build = target['App']['BuildID']
    if target['App']['Name'] != 'Sora' or not re.fullmatch(r'0\.\d+\.\d+(?:\.\d+)?', version) or not re.fullmatch(r'\d{14}', build):
        raise RuntimeError('Invalid target identity')
    args.output.mkdir(parents=True, exist_ok=True)
    feed = args.output / 'isolated-feed'
    feed.mkdir()
    profile = args.output / 'isolated-profile'
    profile.mkdir()
    with args.mar.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha512').hexdigest()
    updates = ET.Element('updates')
    update = ET.SubElement(updates, 'update', {
        'type': 'minor', 'displayVersion': version, 'appVersion': version,
        'platformVersion': target['Gecko']['MaxVersion'].removesuffix('.*'), 'buildID': build,
    })
    ET.SubElement(update, 'patch', {
        'type': 'complete', 'URL': f'https://github.com/tongocharly/sora-browserr/releases/download/v{version}/{args.mar.name}',
        'hashFunction': 'sha512', 'hashValue': digest, 'size': str(args.mar.stat().st_size),
    })
    ET.ElementTree(updates).write(feed / 'update.xml', encoding='utf-8', xml_declaration=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(feed))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    update_url = f'http://127.0.0.1:{server.server_port}/update.xml'
    policies_path = args.baseline / 'distribution/policies.json'
    policies = json.loads(policies_path.read_text()) if policies_path.exists() else {'policies': {}}
    policies.setdefault('policies', {})['AppUpdateURL'] = update_url
    policies_path.parent.mkdir(exist_ok=True)
    policies_path.write_text(json.dumps(policies))
    with socket.socket() as temporary:
        temporary.bind(('127.0.0.1', 0))
        port = temporary.getsockname()[1]
    preferences = {
        'zen.welcome-screen.seen': True, 'browser.shell.checkDefaultBrowser': False,
        'marionette.enabled': True, 'marionette.port': port,
        'app.update.log': True, 'sora.updates.last-check': '0',
        # Marionette normally disables updates for unrelated browser tests.
        # This isolated profile specifically exercises the signed updater.
        'app.update.disabledForTesting': False,
        'intl.locale.requested': 'fr,en-US',
    }
    (profile / 'user.js').write_text(''.join(f'user_pref({json.dumps(k)}, {json.dumps(v)});\n' for k, v in preferences.items()))
    client = None
    process = None
    observed = []
    report = {'targetVersion': version, 'targetBuildID': build, 'signatureBypass': False, 'insecureCertificateAcceptance': False}
    # Enable privileged automation only inside this disposable CI process.
    os.environ['MOZ_REMOTE_ALLOW_SYSTEM_ACCESS'] = '1'
    # Query the actual native checker in a fresh, policy-free target installation.
    probe_profile = args.output / 'production-feed-profile'
    probe_profile.mkdir()
    (probe_profile / 'user.js').write_text((profile / 'user.js').read_text())
    with (args.output / 'PRODUCTION-FEED-browser.log').open('wb') as probe_log:
        probe = subprocess.Popen([str(args.target / 'sora.exe'), '--no-remote', '--marionette',
            '--remote-allow-system-access', '--profile', str(probe_profile), 'about:blank'],
            stdout=probe_log, stderr=subprocess.STDOUT)
        probe_client = None
        try:
            probe_client = connect(port)
            report['candidateIdentity'] = identity(probe_client)
            if report['candidateIdentity']['version'] != version or report['candidateIdentity']['buildID'] != build:
                raise RuntimeError('The running candidate does not have the requested native version and build identity')
            production_url = probe_client.script('''
              const done = arguments[arguments.length - 1];
              Cc['@mozilla.org/updates/update-checker;1'].getService(Ci.nsIUpdateChecker)
                .getUpdateURL(Ci.nsIUpdateChecker.BACKGROUND_CHECK).then(done);
            ''', asynchronous=True)
            report['productionUpdateURL'] = production_url
            if production_url != 'https://github.com/tongocharly/sora-browserr/releases/latest/download/windows-x64-update.xml':
                raise RuntimeError('The production native checker does not use the Sora feed: ' + str(production_url))
        finally:
            if probe_client:
                try:
                    probe_client.script('Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);')
                except (OSError, EOFError, RuntimeError):
                    pass
                probe_client.close()
            try:
                probe.wait(timeout=30)
            except subprocess.TimeoutExpired:
                probe.kill()
                raise RuntimeError('Production feed check could not close Sora cleanly')
    print('Production Sora native update URL verified', flush=True)
    try:
        with (args.output / 'AUTOMATIC-UPDATE-browser.log').open('wb') as output:
            environment = dict(os.environ)
            environment.pop('MOZ_HEADLESS', None)
            process = subprocess.Popen([
                str(args.baseline / 'sora.exe'), '--no-remote', '--marionette',
                '--remote-allow-system-access', '--profile', str(profile), '--width', '1280', '--height', '800', 'about:blank',
            ], env=environment, stdout=output, stderr=subprocess.STDOUT)
            client = connect(port)
            report['before'] = identity(client)
            if report['before']['os'] != 'WINNT' or report['before']['name'] != 'Sora' or report['before']['buildID'] >= build:
                raise RuntimeError('The baseline is not an older Windows Sora build')
            deadline = time.monotonic() + 60
            while not client.script("return Boolean(ChromeUtils.importESModule('resource:///modules/SoraUpdateService.sys.mjs').SoraUpdateService._initialized);"):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Normal Windows startup did not initialize Sora automatic updates')
                time.sleep(1)
            setup = client.script('''
              const done = arguments[arguments.length - 1];
              const {SoraUpdateService} = ChromeUtils.importESModule('resource:///modules/SoraUpdateService.sys.mjs');
              SoraUpdateService.setAutomatic(true).then(async () => done({isWindows: SoraUpdateService.isWindows,
                initialized: SoraUpdateService._initialized, updateURL: await Cc['@mozilla.org/updates/update-checker;1']
                  .getService(Ci.nsIUpdateChecker).getUpdateURL(Ci.nsIUpdateChecker.BACKGROUND_CHECK)}))
                .catch(error => done({error: String(error)}));
            ''', asynchronous=True)
            report['setup'] = setup
            if not setup.get('isWindows') or not setup.get('initialized') or setup.get('updateURL') != update_url:
                raise RuntimeError('The native Sora scheduler or isolated enterprise test feed is unavailable')
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                state = client.script('''
                  const {SoraUpdateService} = ChromeUtils.importESModule('resource:///modules/SoraUpdateService.sys.mjs');
                  return {phase:SoraUpdateService.state.phase, message:SoraUpdateService.message};
                ''')
                if not observed or observed[-1] != state['phase']:
                    observed.append(state['phase'])
                    print('Automatic update:', state['phase'], flush=True)
                if state['phase'] == 'ready':
                    break
                if state['phase'] in {'error','unavailable','managed','unsupported','manual'}:
                    raise RuntimeError('Native automatic update failed: ' + json.dumps(state))
                time.sleep(1)
            else:
                raise RuntimeError('Automatic download and staging timed out')
            try:
                client.script("ChromeUtils.importESModule('resource:///modules/SoraUpdateService.sys.mjs').SoraUpdateService.install();")
            except (EOFError, OSError):
                pass
            client.close()
            client = None
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                try:
                    client = Marionette(port)
                    after = identity(client)
                    if after['buildID'] == build:
                        report['after'] = after
                        break
                    client.close()
                    client = None
                except (OSError, EOFError, RuntimeError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError('Sora did not restart into the target build')
            if report['after']['platformVersion'] != target['Gecko']['MaxVersion'].removesuffix('.*'):
                raise RuntimeError('The updated Gecko engine version differs from the package')
            if report['after']['version'] != version:
                raise RuntimeError('The updated application version differs from the signed release')
            if not {'fr','en-US'}.issubset(report['after']['locales']):
                raise RuntimeError('The updated application lost a bundled interface language')
            try:
                client.command('Marionette:SetContext', {'value': 'content'})
                client.command('WebDriver:Navigate', {'url': 'https://example.com/'})
                client.command('Marionette:SetContext', {'value': 'chrome'})
                screenshot = client.command('WebDriver:TakeScreenshot', {'full': False})
                (args.output / 'WINDOWS-SORA.png').write_bytes(base64.b64decode(screenshot, validate=True))
            except RuntimeError as error:
                report['screenshotError'] = str(error)
            report['passed'] = True
            print('Automatic signed update, restart and new engine identity passed', flush=True)
    finally:
        report['phases'] = observed
        (args.output / 'AUTOMATIC-UPDATE-VERIFICATION.json').write_text(json.dumps(report, indent=2))
        if client:
            try:
                client.script('Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);')
            except (OSError, EOFError, RuntimeError):
                pass
            client.close()
        if process and process.poll() is None:
            process.terminate()
        server.shutdown()


if __name__ == '__main__':
    main()
