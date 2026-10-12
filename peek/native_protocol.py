"""Bounded subprocess transport shared by the laptop and read-only exporter."""
import json
import os
import selectors
import subprocess
import struct
import zlib
import tempfile
import time

MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 12 * 1024 * 1024
MAX_PNG = 8 * 1024 * 1024
WIDTH, HEIGHT = 1600, 1200


def validate_png(data):
    if (not isinstance(data, bytes) or not 45 <= len(data) <= MAX_PNG
            or data[:8] != b'\x89PNG\r\n\x1a\n'):
        raise ValueError('invalid or oversized preview PNG')
    offset = 8
    saw_idat = False
    first = True
    while offset + 12 <= len(data):
        length = struct.unpack('>I', data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            break
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:end - 4]
        crc = struct.unpack('>I', data[end - 4:end])[0]
        if zlib.crc32(kind + body) != crc:
            break
        if first:
            if kind != b'IHDR' or length != 13:
                break
            w, h = struct.unpack('>II', body[:8])
            if not 0 < w <= WIDTH or not 0 < h <= HEIGHT:
                raise ValueError('preview PNG dimensions exceed the limit')
            first = False
        elif kind == b'IHDR':
            break
        if kind == b'IDAT':
            saw_idat = True
        if kind == b'IEND':
            if length == 0 and saw_idat and end == len(data):
                return data
            break
        offset = end
    raise ValueError('invalid or truncated preview PNG')


def run_bounded(argv, data=b'', env=None, timeout=90, limit=MAX_RESPONSE):
    if len(data) > MAX_REQUEST:
        raise ValueError('request exceeds the size limit')
    # A file stdin avoids blocking on a peer that refuses to read its request.
    with tempfile.TemporaryFile() as request:
        request.write(data)
        request.seek(0)
        with subprocess.Popen(argv, stdin=request, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env) as proc:
            streams = {proc.stdout: bytearray(), proc.stderr: bytearray()}
            deadline = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    for stream in streams:
                        selector.register(stream, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError('helper timed out')
                        for key, _ in selector.select(min(remaining, 0.25)):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            buffer = streams[key.fileobj]
                            buffer.extend(chunk)
                            cap = limit if key.fileobj is proc.stdout else 64 * 1024
                            if len(buffer) > cap:
                                raise ValueError('helper response exceeds the size limit')
                    proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                if proc.returncode:
                    error = streams[proc.stderr].decode('utf-8', 'replace')[:400]
                    raise ValueError('helper failed: ' + error)
                return bytes(streams[proc.stdout])
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()


def decode_response(data):
    if len(data) > MAX_RESPONSE:
        raise ValueError('response exceeds the size limit')
    result = json.loads(data)
    if not isinstance(result, dict) or result.get('version') != 1:
        raise ValueError('incompatible Peek exporter; update Peek on the server')
    if result.get('ok') is not True:
        raise ValueError(str(result.get('error') or 'image export failed')[:400])
    if 'candidates' in result:
        candidates = result['candidates']
        if not isinstance(candidates, list) or len(candidates) > 256:
            raise ValueError('invalid candidate list from the exporter')
        kinds = {'image', 'anim', 'video', 'audio', 'pdf', 'office', 'markdown', 'html',
                 'text', 'csv', 'json', 'jsonl', 'notebook', 'sqlite', 'archive', 'binary'}
        for item in candidates:
            if not isinstance(item, dict):
                raise ValueError('invalid candidate from the exporter')
            path = item.get('path')
            sig = item.get('signature')
            if (not isinstance(path, str) or not os.path.isabs(path) or '\x00' in path
                    or not isinstance(item.get('kind'), str) or item['kind'] not in kinds
                    or any(type(item.get(key)) is not int or item[key] < 0 for key in ('row', 'col'))
                    or not isinstance(sig, dict) or set(sig) != {'dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'}
                    or any(type(value) is not int for value in sig.values())
                    or not 0 <= sig['size'] <= 32 * 1024 * 1024):
                raise ValueError('invalid candidate metadata from the exporter')
    return result
