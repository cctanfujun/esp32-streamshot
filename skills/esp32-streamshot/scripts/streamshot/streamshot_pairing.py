"""Explicit first-use computer trust on a trusted LAN; no code or owner UI.

This authenticates subsequent phone requests, not the human owner of a newly
registering LAN client. Each grant has its own principal; shared legacy or agent
credentials are never returned. Only hashes and bounded metadata are persisted.
"""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

MAX_CLIENTS = 8
NEW_PER_WINDOW = 5
UNUSED_SECONDS = 300
RATE_SECONDS = 60


class PairingFailure(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def exact(value, keys):
    if (not isinstance(value, dict) or set(value) != set(keys)
            or type(value.get('v')) is not int or value['v'] != 2):
        raise PairingFailure('invalid_pairing_request')


def canonical_id(value):
    if not isinstance(value, str):
        raise PairingFailure('invalid_pairing_request')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise PairingFailure('invalid_pairing_request') from None
    return value


def hex_value(value):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise PairingFailure('invalid_pairing_request')
    return value


def digest(value):
    return hashlib.sha256(bytes.fromhex(value)).hexdigest()


class PairingService:
    def __init__(self, phone_configuration, directory=None, *, clock=time.time, available=lambda: True):
        if set(phone_configuration) != {'version', 'name', 'baseURL', 'uploadToken', 'certificateSHA256'}:
            raise ValueError('Invalid phone configuration')
        self.phone = dict(phone_configuration)
        self.clock, self.available = clock, available
        self.lock = threading.RLock()
        self.grants = []
        self.recent = []
        self.path = Path(directory) / 'phones.json' if directory is not None else None
        if self.path is not None and self.path.exists():
            from streamshot_bridge import decode, private_file
            value = decode(private_file(self.path).read_bytes())
            if (not isinstance(value, dict) or set(value) != {'version', 'grants'}
                    or type(value['version']) is not int or value['version'] != 2
                    or not isinstance(value['grants'], list) or len(value['grants']) > MAX_CLIENTS):
                raise ValueError('Invalid phone registry')
            ids, clients = set(), set()
            for item in value['grants']:
                if not isinstance(item, dict) or set(item) != {'id', 'client_id', 'request_id', 'secretHash', 'tokenHash', 'created', 'used'}:
                    raise ValueError('Invalid phone registry')
                for field in ('id', 'client_id', 'request_id'):
                    canonical_id(item[field])
                for field in ('secretHash', 'tokenHash'):
                    hex_value(item[field])
                if (type(item['created']) not in (int, float) or not math.isfinite(item['created']) or item['created'] < 0
                        or type(item['used']) is not bool or item['id'] in ids or item['client_id'] in clients):
                    raise ValueError('Invalid phone registry')
                ids.add(item['id']); clients.add(item['client_id'])
            self.grants = value['grants']
            self._prune()

    def _save(self):
        if self.path is None:
            return
        # Parent/state ownership and permissions were already validated by serve.
        temp = self.path.with_name('.phones-' + str(uuid.uuid4()) + '.tmp')
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 2, 'grants': self.grants}, stream, separators=(',', ':'), allow_nan=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _prune(self):
        old = self.grants
        self.grants = [g for g in old if g['used'] or self.clock() - g['created'] < UNUSED_SECONDS]
        if len(self.grants) != len(old):
            self._save()

    def identity(self, value):
        exact(value, {'v'})
        return {'v': 2, **{key: self.phone[key] for key in ('name', 'baseURL', 'certificateSHA256')}}

    def _token(self, value):
        # A CSPRNG client secret plus a private CSPRNG server key yields a
        # separate 256-bit role credential. Retries need the original secret;
        # public IDs alone cannot retrieve or overwrite an existing grant.
        message = b'StreamShot-phone-grant-v2\0' + b'\0'.join(value[key].encode('ascii')
                   for key in ('client_id', 'request_id', 'client_secret'))
        return hmac.new(bytes.fromhex(self.phone['uploadToken']), message, hashlib.sha256).hexdigest()

    def trust(self, value):
        exact(value, {'v', 'client_id', 'request_id', 'client_secret', 'certificateSHA256'})
        canonical_id(value['client_id']); canonical_id(value['request_id'])
        hex_value(value['client_secret']); hex_value(value['certificateSHA256'])
        with self.lock:
            if not self.available():
                raise PairingFailure('pairing_busy', 409)
            if not hmac.compare_digest(value['certificateSHA256'], self.phone['certificateSHA256']):
                raise PairingFailure('pairing_verification_failed', 403)
            self._prune()
            secret_hash = digest(value['client_secret'])
            token = self._token(value)
            previous = next((g for g in self.grants if g['client_id'] == value['client_id']
                             or g['request_id'] == value['request_id']), None)
            if previous is not None:
                if (previous['client_id'] != value['client_id'] or previous['request_id'] != value['request_id']
                        or not hmac.compare_digest(previous['secretHash'], secret_hash)
                        or not hmac.compare_digest(previous['tokenHash'], digest(token))):
                    raise PairingFailure('pairing_conflict', 409)
            else:
                now = self.clock()
                self.recent = [t for t in self.recent if now - t < RATE_SECONDS]
                if len(self.recent) >= NEW_PER_WINDOW or len(self.grants) >= MAX_CLIENTS:
                    raise PairingFailure('pairing_rate_limited', 429)
                self.recent.append(now)
                grant = {'id': str(uuid.uuid4()), 'client_id': value['client_id'], 'request_id': value['request_id'],
                         'secretHash': secret_hash, 'tokenHash': digest(token), 'created': now, 'used': False}
                self.grants.append(grant)
                try:
                    self._save()
                except Exception:
                    self.grants.remove(grant)
                    raise
            if not self.available():
                raise PairingFailure('pairing_busy', 409)
            return {'v': 2, 'client_id': value['client_id'], 'request_id': value['request_id'],
                    'configuration': dict(self.phone, uploadToken=token)}

    def authenticate(self, token):
        if not isinstance(token, str) or re.fullmatch('[a-f0-9]{64}', token) is None:
            return None
        with self.lock:
            self._prune()
            wanted = digest(token)
            for grant in self.grants:
                if hmac.compare_digest(grant['tokenHash'], wanted):
                    if not grant['used']:
                        grant['used'] = True
                        try:
                            self._save()
                        except Exception:
                            grant['used'] = False
                            raise
                    return grant['id']
        return None

    def close(self):
        # Persisted hashes support future connections. No raw grant secret is held.
        with self.lock:
            self.recent.clear()
