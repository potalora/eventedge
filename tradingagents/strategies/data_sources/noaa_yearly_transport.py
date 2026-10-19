"""Standalone bounded NOAA HTTP worker; parent owns hard deadline and cleanup.

Execute by file path to avoid importing the application in a network child.
Only safe bounded status metadata is emitted; response bodies stay on disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os

import httpx

CHUNK_BYTES = 65536


def acquire(url, output, max_bytes, connect_timeout, read_timeout):
    body = None
    try:
        timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout,
            write=read_timeout, pool=connect_timeout)
        with httpx.Client(trust_env=False, timeout=timeout, follow_redirects=False) as client:
            with client.stream('GET', url, headers={'Accept-Encoding': 'identity'}) as response:
                # Retain only bounded headers consumed by the owning request policy.
                headers = {name: response.headers[name] for name in
                    ('Content-Length', 'Content-Encoding', 'Retry-After')
                    if name in response.headers and len(response.headers[name]) <= 64}
                result = {'status_code': response.status_code, 'headers': headers}
                if not 200 <= response.status_code < 300:
                    return result
                declared = response.headers.get('Content-Length')
                if declared is not None and (not declared.isascii() or not declared.isdigit()
                        or len(declared) > 12 or int(declared) > max_bytes):
                    return {'reason_code': 'invalid_response'}
                if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                    return {'reason_code': 'invalid_response'}
                descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                body = os.fdopen(descriptor, 'wb')
                received, digest = 0, hashlib.sha256()
                with body:
                    for chunk in response.iter_raw(chunk_size=CHUNK_BYTES):
                        received += len(chunk)
                        if received > max_bytes:
                            return {'reason_code': 'invalid_response'}
                        digest.update(chunk)
                        body.write(chunk)
                if not received or declared is not None and received != int(declared):
                    return {'reason_code': 'invalid_response'}
                return {**result, 'bytes': received, 'sha256': digest.hexdigest()}
    except httpx.TimeoutException:
        return {'reason_code': 'timeout'}
    except httpx.TransportError:
        return {'reason_code': 'transport_error'}
    except Exception:
        return {'reason_code': 'provider_error'}
    finally:
        if body is not None:
            body.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--max-bytes', required=True, type=int)
    parser.add_argument('--connect-timeout', required=True, type=float)
    parser.add_argument('--read-timeout', required=True, type=float)
    args = parser.parse_args()
    result = acquire(args.url, args.output, args.max_bytes, args.connect_timeout, args.read_timeout)
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
