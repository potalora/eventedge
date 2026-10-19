"""Private closed SEC originals and one finite physical-storage owner.

The ledger charges an original inode once even after its parent name is removed.
Anonymous child copies have separate leases, released only by the dispatcher
following a closed-job acknowledgement or confirmed process reaping.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import gzip
import json
import errno
import math
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
from types import MappingProxyType

from .fetch_errors import SourceFetchError, source_fetch_error
from .request_policy import provider_clock_time, provider_timeout

POLICY = 'bounded_original_submission_v1'
MAX_SUBMISSION_BYTES = 512 * 1024**2
_CURRENT = ContextVar('sec_submission_spool', default=None)


def _failure(reason='invalid_response'):
    return SourceFetchError('SEC submission spool failed', reason_code=reason)


def _encoded_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
            info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True, eq=False)
class CompletedSubmission:
    path: Path
    identity: object
    observed_at: str
    size: int
    sha256: str
    owner: 'SpoolScope'
    complete: bool = True


@dataclass(frozen=True, eq=False)
class RetainedLease:
    path: Path
    record: CompletedSubmission


@dataclass(frozen=True, eq=False)
class EncodedOriginalLease:
    path: Path
    record: CompletedSubmission
    size: int
    sha256: str
    encoding: str


class _EncodedWriter:
    """Unbuffered gzip sink; capture reservation always precedes owner locking."""
    def __init__(self, owner, entry, reserve_external):
        self.owner, self.entry = owner, entry
        self.reserve_external = reserve_external
        self.digest = hashlib.sha256()

    def write(self, data):
        for start in range(0, len(data), 65536):
            part = data[start:start+65536]
            self.owner.check()
            # GzipFile may call close() from a failed constructor's destructor.
            # A stale sink must not reserve capture quota or revive its ledger.
            if not self.entry['writing'] or self.entry['fd'] is None:
                raise _failure()
            # The caller holds the capture lock for the entire operation.
            # Calling this under _lock would invert capture -> owner ordering.
            if self.reserve_external is not None:
                self.reserve_external(len(part))
            with self.owner._lock:
                if not self.entry['writing'] or self.entry['fd'] is None:
                    raise _failure()
                self.owner._reserve(len(part))
                self.entry['size'] += len(part)
                self.owner._account()
                offset = 0
                while offset < len(part):
                    self.owner.check()
                    written = os.write(self.entry['fd'], part[offset:])
                    if not written or written > len(part)-offset:
                        raise _failure('transport_error')
                    offset += written
                    self.owner.check()
            self.digest.update(part)
        return len(data)


@dataclass(frozen=True, eq=False)
class ChildCopyLease:
    record: CompletedSubmission
    size: int


class SpoolScope:
    def __init__(self, root, *, original_deadline, physical_limit):
        if (type(original_deadline) not in (int, float) or not math.isfinite(original_deadline)
                or type(physical_limit) is not int or not 0 < physical_limit <= 4 * 1024**3):
            raise ValueError('Invalid SEC spool resource bounds')
        self.original_deadline, self.physical_limit = original_deadline, physical_limit
        self._lock = threading.RLock()
        self._closed, self._closing = False, False
        self._entries, self._records, self._retained, self._children = {}, {}, {}, {}
        self._encoded = {}
        self._reconciliations = {}
        self._counts = {key: 0 for key in ('retained_bytes', 'parent_bytes', 'child_bytes',
            'transient_bytes', 'total_bytes', 'completed_objects', 'failed_objects', 'active_child_copies', 'active_parent_writers', 'active_parent_readers', 'cleanup_failures')}
        self._peaks = {key: 0 for key in ('retained_bytes', 'parent_bytes', 'child_bytes',
            'transient_bytes', 'total_bytes', 'active_child_copies')}
        self.check()
        self.root = Path(tempfile.mkdtemp(prefix='sec-submissions-', dir=root))
        os.chmod(self.root, 0o700)

    def check(self):
        provider_timeout('edgar')
        if provider_clock_time('edgar') >= self.original_deadline:
            raise _failure('timeout')
        if self._closing:
            raise _failure('provider_error')

    def _account(self):
        retained = sum(e['size'] for e in self._entries.values() if e['aliases'])
        parents = sum(e['size'] for e in self._entries.values() if e['parent'] and not e['aliases'])
        children = sum(lease.size for lease in self._children)
        self._counts.update(retained_bytes=retained, parent_bytes=parents, child_bytes=children,
            transient_bytes=parents+children, total_bytes=retained+parents+children,
            active_child_copies=len(self._children), active_parent_writers=sum(
                bool(e['writing'] or e['fd'] is not None) for e in self._entries.values()),
            active_parent_readers=sum(e['readers'] for e in self._entries.values()))
        for key in self._peaks:
            self._peaks[key] = max(self._peaks[key], self._counts[key])

    def stats(self):
        with self._lock:
            return dict(self._counts, closing=self._closing, closed=self._closed, **{'peak_'+key: value for key, value in self._peaks.items()})

    def completed_metadata(self):
        """Copy all closed-source metadata, including subsequent parse failures."""
        with self._lock:
            return [{'identity': dict(record.identity), 'observed_at': record.observed_at,
                     'size': record.size, 'sha256': record.sha256, 'complete': True}
                    for record in self._records]

    def record_reconciliation(self, record, evidence):
        """Attest a returned parse before hydration can reject its projection."""
        from .filing_inventory_reconciliation import validate_reconciled_evidence
        with self._lock:
            self.validate_completed(record)
            original={'identity':dict(record.identity),'sha256':record.sha256,
                'size':record.size,'observed_at':record.observed_at}
            validate_reconciled_evidence(evidence,original=original,check=self.check)
            proof=evidence['inventory_reconciliation']
            encoded=json.dumps(proof,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
            entry={'submission_sha256':record.sha256,'submission_size':record.size,
                'proof_sha256':hashlib.sha256(encoded).hexdigest(),'proof':deepcopy(proof),
                'framing':{key:deepcopy(evidence[key]) for key in ('header_sha256','roles','document_inventory')}}
            accession=record.identity['accession']
            if accession in self._reconciliations:raise _failure()
            self._reconciliations[accession]=entry

    def reconciliation_manifest(self):
        """Closed immutable-result metadata, without paths or narrative text."""
        with self._lock:
            return deepcopy(dict(sorted(self._reconciliations.items())))

    def _reserve(self, size):
        self.check()
        if self._counts['total_bytes'] + size > self.physical_limit:
            raise _failure()

    def _entry(self, record):
        if not isinstance(record, CompletedSubmission) or record.owner is not self or record not in self._records:
            raise _failure()
        return self._records[record]

    def validate_completed(self, record):
        """Validate an owner-issued closed parent without opening arbitrary paths."""
        with self._lock:
            self.check()
            entry = self._entry(record)
            if not entry['parent']:
                raise _failure()
            try:
                info = record.path.lstat()
            except OSError:
                raise _failure('invalid_response') from None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o400 or info.st_size != record.size
                    or (info.st_dev, info.st_ino) != entry['inode']):
                raise _failure()
            return record

    @contextmanager
    def open_completed(self, record):
        """Pin and open only the registered inode, refusing symlink substitution."""
        with self._lock:
            self.validate_completed(record)
            entry = self._entry(record)
            try:
                fd = os.open(record.path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                raise _failure('invalid_response') from None
            reader = {'fd': fd, 'inode': entry['inode'], 'reading': True}
            entry['reader_fds'].append(reader)
            entry['readers'] += 1
            self._account()
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != entry['inode'] or info.st_size != record.size:
                raise _failure()
            # Close the Python wrapper first, then confirm the owned descriptor.
            with os.fdopen(fd, 'rb', buffering=0, closefd=False) as source:
                yield source
        except OSError:
            raise _failure('transport_error') from None
        finally:
            with self._lock:
                reader['reading'] = False
                try:
                    self._close_owned_fd(reader)
                finally:
                    if reader['fd'] is None:
                        entry['reader_fds'].remove(reader)
                        entry['readers'] -= 1
                    self._account()
                    try:
                        if entry['discard']:
                            self.discard(record)
                    finally:
                        self._finish_close()

    def retain_hardlink(self, record, destination):
        with self._lock:
            self.validate_completed(record)
            entry = self._entry(record)
            destination = Path(destination)
            try:
                os.link(record.path, destination, follow_symlinks=False)
            except OSError:
                raise _failure('transport_error') from None
            linked = destination.lstat()
            if (not stat.S_ISREG(linked.st_mode)
                    or (linked.st_dev, linked.st_ino) != entry['inode']
                    or linked.st_size != record.size or stat.S_IMODE(linked.st_mode) != 0o400):
                destination.unlink()
                raise _failure()
            lease = RetainedLease(destination, record)
            entry['aliases'].add(lease)
            self._retained[lease] = entry
            self._account()
            return lease

    def release_retained(self, lease):
        with self._lock:
            entry = self._retained.get(lease)
            if entry is None:
                raise ValueError('Unknown SEC retained lease')
            try:
                lease.path.unlink(missing_ok=True)
            except OSError:
                raise _failure('transport_error') from None
            del self._retained[lease]
            entry['aliases'].remove(lease)
            self._account()

    def retain_encoded(self, record, destination, *, reserve_external=None):
        """Retain one verified gzip original as a separately charged inode.

        A capture caller must hold its capture lock before entering this method.
        Its optional callback reserves capture bytes before each bounded write;
        reservations are deliberately not refunded after an ambiguous failure.
        """
        destination = Path(os.path.abspath(destination))
        entry, lease, close_attempted = None, None, False
        try:
            with self.open_completed(record) as source:
                with self._lock:
                    self.check()
                    existing = self._entries.get(destination)
                    if existing is not None and (existing['parent'] or existing['aliases']
                                                 or existing['fd'] is not None):
                        raise _failure()
                    # Pin the destination directory's identity for publication
                    # and cleanup; never remove a name in a substituted parent.
                    parent = destination.parent.lstat()
                    if not stat.S_ISDIR(parent.st_mode):
                        raise _failure()
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    entry = {'size': 0, 'parent': True, 'aliases': set(), 'inode': None,
                        'children': 0, 'readers': 0, 'reader_fds': [], 'discard': False,
                        'fd': fd, 'writing': True, 'encoded': True,
                        'directory_inode': (parent.st_dev, parent.st_ino)}
                    self._entries[destination] = entry
                    self._account()
                    info = os.fstat(fd)
                    entry['inode'] = (info.st_dev, info.st_ino)
                    self._validate_encoded_name(destination, entry)
                sink = _EncodedWriter(self, entry, reserve_external)
                digest, count = hashlib.sha256(), 0
                # GzipFile emits the fixed canonical header, including OS=255.
                with gzip.GzipFile(filename='', mode='wb', compresslevel=1,
                                   fileobj=sink, mtime=0) as encoded:
                    while True:
                        self.check()
                        chunk = source.read(65536)
                        self.check()
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > record.size:
                            raise _failure()
                        digest.update(chunk)
                        encoded.write(chunk)
                    if count != record.size or digest.hexdigest() != record.sha256:
                        raise _failure()
                self.check()
                os.fchmod(entry['fd'], 0o400)
                with self._lock:
                    close_attempted = True
                    self._close_owned_fd(entry)
                self.check()
                # Independently read the closed output; the write digest alone
                # cannot establish the identity or contents of the published file.
                with self._lock:
                    stored_identity = _encoded_identity(
                        self._validate_encoded_name(destination, entry, closed=True))
                close_attempted = False
                self._verify_encoded_content(destination, entry, sink.digest.digest(), stored_identity)
                with self._lock:
                    close_attempted = True
                    self._close_owned_fd(entry)
                self.check()
            # Even a failed raw reader close prevents lease publication.
            # Recheck contents after that close: a completed same-size rewrite
            # can share mtime/ctime with the earlier readback on some filesystems.
            close_attempted = False
            stored_digest = self._verify_encoded_content(
                destination, entry, sink.digest.digest(), stored_identity)
            with self._lock:
                close_attempted = True
                self._close_owned_fd(entry)
                self.check()
                if _encoded_identity(self._validate_encoded_name(destination, entry,
                                                                closed=True)) != stored_identity:
                    raise _failure()
                lease = EncodedOriginalLease(destination, record, entry['size'],
                    stored_digest, 'gzip_original_v1')
                entry['aliases'].add(lease)
                entry['parent'] = False
                self._encoded[lease] = entry
                self._account()
            return lease
        except Exception as error:
            raise source_fetch_error('SEC submission spool failed', error) from None
        finally:
            if entry is not None:
                with self._lock:
                    cleanup_error = None
                    try:
                        if not close_attempted:
                            self._close_owned_fd(entry)
                    except SourceFetchError as error:
                        cleanup_error = error
                    finally:
                        entry['writing'] = False
                    if lease is None:
                        try:
                            self._discard_entry(destination, entry)
                        except SourceFetchError as error:
                            cleanup_error = error
                    self._account()
                    self._finish_close()
                    if cleanup_error is not None:
                        raise cleanup_error from None

    def _verify_encoded_content(self, destination, entry, expected_digest, stored_identity):
        """Read a pinned inode in bounded chunks, leaving its FD owned by entry.

        The caller holds the capture lock and closes through _close_owned_fd.
        This verifies bytes at this read, not arbitrary same-UID writes after it.
        """
        with self._lock:
            self.check()
            if _encoded_identity(self._validate_encoded_name(destination, entry,
                                                            closed=True)) != stored_identity:
                raise _failure()
            entry['fd'] = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
            if _encoded_identity(os.fstat(entry['fd'])) != stored_identity:
                raise _failure()
        digest, count = hashlib.sha256(), 0
        while True:
            self.check()
            chunk = os.read(entry['fd'], 65536)
            self.check()
            if not chunk:
                break
            count += len(chunk)
            if count > entry['size']:
                raise _failure()
            digest.update(chunk)
        if (count != entry['size'] or digest.digest() != expected_digest
                or _encoded_identity(os.fstat(entry['fd'])) != stored_identity):
            raise _failure()
        return digest.hexdigest()

    def _validate_encoded_name(self, path, entry, *, closed=False):
        """Fail closed on missing/substituted names and destination directories."""
        parent, info = path.parent.lstat(), path.lstat()
        if (not stat.S_ISDIR(parent.st_mode)
                or (parent.st_dev, parent.st_ino) != entry['directory_inode']
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != entry['inode']
                or (closed and (info.st_size != entry['size']
                                or stat.S_IMODE(info.st_mode) != 0o400))):
            raise _failure()
        return info

    def _unlink_encoded(self, path, entry):
        try:
            self._validate_encoded_name(path, entry)
            path.unlink()
            # A successful syscall alone is not proof that our name is absent.
            try:
                path.lstat()
            except FileNotFoundError:
                return
            raise _failure()
        except (OSError, SourceFetchError):
            self._counts['cleanup_failures'] += 1
            raise _failure('transport_error') from None

    def release_encoded(self, lease):
        with self._lock:
            entry = self._encoded.get(lease)
            if entry is None:
                raise ValueError('Unknown SEC encoded-original lease')
            self._unlink_encoded(lease.path, entry)
            del self._encoded[lease]
            entry['aliases'].remove(lease)
            self._account()

    def discard(self, record):
        with self._lock:
            entry = self._entry(record)
            try:
                self._discard_entry(record.path, entry)
            finally:
                self._account()

    def _discard_entry(self, path, entry):
        entry['discard'] = True
        if (entry['parent'] and not entry['children'] and not entry['readers']
                and not entry['writing'] and entry['fd'] is None):
            if entry.get('encoded'):
                self._unlink_encoded(path, entry)
                entry['parent'] = False
                return
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self._counts['cleanup_failures'] += 1
                raise _failure('transport_error') from None
            entry['parent'] = False

    def _close_owned_fd(self, entry):
        """Attempt once; retain ownership and charge unless closure is confirmed."""
        fd = entry['fd']
        if fd is None:
            return
        try:
            info = os.fstat(fd)
        except OSError as error:
            if error.errno == errno.EBADF:
                entry['fd'] = None
                return
            info = None
        if (info is not None and entry['inode'] is not None
                and (info.st_dev, info.st_ino) != entry['inode']):
            # A previously closed descriptor has been reused; never close it.
            entry['fd'] = None
            return
        try:
            os.close(fd)
        except OSError:
            try:
                info = os.fstat(fd)
            except OSError as error:
                if error.errno == errno.EBADF:
                    entry['fd'] = None
            else:
                if entry['inode'] is not None and (info.st_dev, info.st_ino) != entry['inode']:
                    entry['fd'] = None
            self._counts['cleanup_failures'] += 1
            raise _failure('transport_error') from None
        entry['fd'] = None

    def reserve_child_copy(self, record):
        with self._lock:
            self._entry(record)
            self._reserve(record.size)
            lease = ChildCopyLease(record, record.size)
            self._children[lease] = True
            self._entry(record)['children'] += 1
            self._account()
            return lease

    def release_child_copy(self, lease):
        with self._lock:
            if lease not in self._children:
                raise ValueError('Unknown SEC child-copy lease')
            del self._children[lease]
            entry = self._entry(lease.record)
            entry['children'] -= 1
            try:
                if entry['discard']:
                    self.discard(lease.record)
            finally:
                self._account()
                self._finish_close()

    def _finish_close(self):
        if (self._closing and not self._counts['active_parent_writers']
                and not self._counts['active_parent_readers'] and not self._children
                and not any(e['parent'] or e['fd'] is not None for e in self._entries.values())):
            try:
                self.root.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                self._counts['cleanup_failures'] += 1
                raise _failure('transport_error') from None
            self._closed = True

    def close(self):
        with self._lock:
            self._closing = True
            failed = False
            for path, entry in self._entries.items():
                try:
                    if not entry['writing']:
                        self._close_owned_fd(entry)
                except SourceFetchError:
                    failed = True
                for reader in list(entry['reader_fds']):
                    if reader['reading']:
                        continue
                    try:
                        self._close_owned_fd(reader)
                    except SourceFetchError:
                        failed = True
                    finally:
                        if reader['fd'] is None:
                            entry['reader_fds'].remove(reader)
                            entry['readers'] -= 1
                try:
                    self._discard_entry(path, entry)
                except SourceFetchError:
                    failed = True
            self._account()
            self._finish_close()
            if failed or not self._closed:
                if not failed:
                    self._counts['cleanup_failures'] += 1
                raise _failure('provider_error')

    def spool_response(self, response, *, identity, max_bytes):
        fd, path, entry, receipt = None, None, None, None
        closed_response = False
        close_attempted = False
        try:
            if type(max_bytes) is not int or not 0 < max_bytes <= MAX_SUBMISSION_BYTES:
                raise ValueError('Invalid SEC submission byte limit')
            if (not isinstance(identity, dict) or set(identity) != {'accession', 'form', 'filing_date', 'source_url'}
                    or any(not isinstance(v, str) or not v or len(v) > 2048 for v in identity.values())):
                raise _failure()
            identity = MappingProxyType(dict(identity))
            with self._lock:
                self.check()
                path = self.root / (secrets.token_hex(24)+'.bin')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                entry = {'size': 0, 'parent': True, 'aliases': set(), 'inode': None,
                    'children': 0, 'readers': 0, 'reader_fds': [], 'discard': False,
                    'fd': fd, 'writing': True}
                self._entries[path] = entry
                self._account()
                info = os.fstat(fd)
                entry['inode'] = (info.st_dev, info.st_ino)
            digest = hashlib.sha256()
            chunks = iter(response.iter_content(chunk_size=65536))
            while True:
                self.check()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    self.check()
                    break
                self.check()
                if not isinstance(chunk, bytes):
                    raise _failure()
                if entry['size'] + len(chunk) > max_bytes:
                    raise _failure()
                # Even nonconforming response iterators cannot cause a write,
                # hash update, or reservation unit larger than 64 KiB.
                for start in range(0, len(chunk), 65536):
                    part = chunk[start:start+65536]
                    with self._lock:
                        self._reserve(len(part))
                        entry['size'] += len(part)
                        self._account()
                        offset = 0
                        while offset < len(part):
                            self.check()
                            written = os.write(fd, part[offset:])
                            if not written:
                                raise _failure('transport_error')
                            offset += written
                            self.check()
                    digest.update(part)
                    self.check()
            self.check()
            if entry['size'] == 0:
                raise _failure()
            os.fchmod(fd, 0o400)
            with self._lock:
                close_attempted = True
                self._close_owned_fd(entry)
            self.check()
            closed_response = True
            response.close()
            self.check()
            with self._lock:
                self.check()
                receipt = CompletedSubmission(path, identity, datetime.now(timezone.utc).isoformat(),
                    entry['size'], digest.hexdigest(), self)
                self._records[receipt] = entry
                self._counts['completed_objects'] += 1
            return receipt
        except Exception as error:
            raise source_fetch_error('SEC submission spool failed', error) from None
        finally:
            if not closed_response:
                try:
                    response.close()
                except Exception:
                    pass
            with self._lock:
                cleanup_error = None
                if receipt is None:
                    self._counts['failed_objects'] += 1
                if entry is not None:
                    try:
                        if not close_attempted:
                            self._close_owned_fd(entry)
                    except SourceFetchError as error:
                        cleanup_error = error
                    finally:
                        entry['writing'] = False
                    if receipt is None or entry['discard']:
                        try:
                            self._discard_entry(path, entry)
                        except SourceFetchError as error:
                            cleanup_error = error
                self._account()
                self._finish_close()
                if cleanup_error is not None:
                    raise cleanup_error from None


@contextmanager
def submission_spool_scope(root, *, original_deadline, physical_limit=4 * 1024**3):
    if _CURRENT.get() is not None:
        raise ValueError('Nested SEC submission spool scope is not supported')
    owner = SpoolScope(root, original_deadline=original_deadline, physical_limit=physical_limit)
    token = _CURRENT.set(owner)
    try:
        yield owner
    finally:
        _CURRENT.reset(token)
        owner.close()


def current_submission_spool():
    return _CURRENT.get()


def spool_response(response, *, identity, max_bytes=MAX_SUBMISSION_BYTES):
    owner = current_submission_spool()
    if owner is None:
        raise ValueError('SEC submission spool scope is not active')
    return owner.spool_response(response, identity=identity, max_bytes=max_bytes)


def completed_submission(record):
    """Default closed-source observation seam; no raw archive is made here."""
