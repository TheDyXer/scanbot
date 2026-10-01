"""
Scans as jobs on disk, so that a restart (an update, a crash, a reboot) resumes them instead of losing them.

Each job is two files in <state folder>/jobs/: <id>.json, rewritten whole (atomically) whenever the scan saves its
progress, and <id>.list.txt, the addresses to scan, written once and deleted when the scan ends. Nothing here knows
about Discord: bot.py turns jobs into scans.
"""
import dataclasses
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger('scanbot')

FORMAT_VERSION = 1
UNFINISHED = ('queued', 'running', 'interrupted')  # Resumed after a restart
ENDED = ('done', 'stopped', 'abandoned')          # Kept a while (KEEP_FINISHED_PER_USER), then deleted


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


@dataclasses.dataclass
class Job:
    id: str
    owner_id: int
    owner_mention: str
    guild_id: Optional[int]       # Server it was started in (None in DMs)
    channel_id: Optional[int]     # Where its messages go
    edition: str
    api_retry: bool
    source: dict                  # {'kind': 'file' or 'target', 'description': ...}
    total: int                    # Addresses in the list
    notes: str = ''               # The start message's notes: expanded ranges, skipped lines, duplicates
    status: str = 'queued'        # queued, running, interrupted, done, stopped or abandoned
    progress_message_id: Optional[int] = None
    # How far it got: the phase ('direct', 'api', 'geo' or 'send') and, in the first two, how many entries of that
    # phase's list are done (every entry before `index` is, some after it may be too)
    cursor: dict = dataclasses.field(default_factory=lambda: {'phase': 'direct', 'index': 0})
    blocked: list = dataclasses.field(default_factory=list)   # Entries skipped: names of private addresses
    unchecked: int = 0            # Servers no API could check
    results: dict = dataclasses.field(default_factory=dict)   # Entry -> online server, as bot.make_result makes it
    locations: dict = dataclasses.field(default_factory=dict)  # IP -> country code (saved when it ends)
    networks: dict = dataclasses.field(default_factory=dict)   # IP -> [AS number, organisation] (same)
    timings: dict = dataclasses.field(default_factory=lambda: {'direct': None, 'api': None})  # [checked, seconds]
    elapsed: float = 0.0          # Seconds it has run, over all its runs
    resumed: int = 0              # Times it was resumed after a restart
    stopped_by: Optional[int] = None
    error: bool = False           # It stopped because the bot hit an error
    created_at: str = ''
    updated_at: str = ''
    finished_at: Optional[str] = None
    version: int = FORMAT_VERSION

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data):
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})

    @property
    def finished(self):
        return self.status in ENDED


def new_job(owner_id, owner_mention, guild_id, channel_id, edition, api_retry, source, total, notes=''):
    """A new scan's job. Its id sorts by time and names the owner: 20261001T031500Z-123456789-ab12."""
    created = datetime.now(timezone.utc)
    job = Job(id=f"{created:%Y%m%dT%H%M%SZ}-{owner_id}-{secrets.token_hex(2)}", owner_id=owner_id,
              owner_mention=owner_mention, guild_id=guild_id, channel_id=channel_id, edition=edition,
              api_retry=api_retry, source=source, total=total, notes=notes,
              created_at=created.strftime('%Y-%m-%dT%H:%M:%SZ'))
    job.updated_at = job.created_at
    return job


class JobStore:
    """The jobs folder. Every method that writes logs a failure instead of raising: a full disk mustn't end a scan."""

    def __init__(self, directory):
        self.directory = directory
        self.jobs_dir = os.path.join(directory, 'jobs')
        self.save_failed = False  # Warn once, not on every save

    @classmethod
    def open(cls, directory):
        """The store in `directory`, or None, after one warning saying how to fix it, if it can't be written."""
        jobs_dir = os.path.join(directory, 'jobs')
        try:
            os.makedirs(jobs_dir, exist_ok=True)
            probe = os.path.join(jobs_dir, f'.write-test-{os.getpid()}')
            with open(probe, 'w') as f:
                f.write('ok')
            os.remove(probe)
        except OSError as e:
            ids = f"{os.getuid()}:{os.getgid()}" if hasattr(os, 'getuid') else None
            fix = (f"With Docker, run this in the scanbot folder:  sudo mkdir -p state && sudo chown {ids} state  "
                   "(the installer does it too), then restart the bot. " if ids else "")
            fix += "Without Docker, set STATE_DIR to a folder the bot can write to."
            log.warning("Scans can't be saved in %s (%s), so a restart ends them instead of resuming them. %s",
                        directory, e.strerror or e, fix)
            return None
        return cls(directory)

    def path(self, job, suffix='.json'):
        return os.path.join(self.jobs_dir, job.id + suffix)

    new_job = staticmethod(new_job)

    def write(self, path, text):
        """Writes the whole file or nothing: a crash halfway leaves the old one."""
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(tmp, path)

    def save(self, job):
        """Saves the job. Returns False (after one warning) if it couldn't."""
        job.updated_at = now()
        try:
            self.write(self.path(job), json.dumps(job.to_dict(), ensure_ascii=False, separators=(',', ':')))
        except (OSError, TypeError, ValueError) as e:
            if not self.save_failed:
                log.warning("Couldn't save scan %s (%s); it won't resume after a restart", job.id, e)
                self.save_failed = True
            return False
        self.save_failed = False
        return True

    def write_list(self, job, entries):
        try:
            self.write(self.path(job, '.list.txt'), '\n'.join(entries) + '\n')
        except OSError as e:
            log.warning("Couldn't save the list of scan %s (%s); it won't resume after a restart", job.id, e)
            return False
        return True

    def read_list(self, job):
        """The job's addresses, or None if the file is gone."""
        try:
            with open(self.path(job, '.list.txt'), encoding='utf-8') as f:
                return f.read().splitlines()
        except OSError:
            return None

    def remove_list(self, job):
        try:
            os.remove(self.path(job, '.list.txt'))
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Couldn't delete the list of scan %s: %s", job.id, e)

    def delete(self, job):
        for suffix in ('.json', '.list.txt'):
            try:
                os.remove(self.path(job, suffix))
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("Couldn't delete %s: %s", self.path(job, suffix), e)

    def all(self, owner_id=None):
        """
        Every job, or one user's, oldest first. A file that can't be read is renamed to .broken and skipped. The file
        names carry the owner (see new_job), so one user's jobs are found without reading anyone else's.
        """
        found = []
        try:
            names = sorted(os.listdir(self.jobs_dir))
        except OSError as e:
            log.warning("Couldn't read the scans in %s: %s", self.jobs_dir, e)
            return found
        for name in names:
            if not name.endswith('.json') or (owner_id is not None and f'-{owner_id}-' not in name):
                continue
            path = os.path.join(self.jobs_dir, name)
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                if data.get('version', FORMAT_VERSION) > FORMAT_VERSION:
                    log.warning("Skipping %s: it was saved by a newer version of the bot", name)
                    continue
                found.append(Job.from_dict(data))
            except (OSError, ValueError, TypeError) as e:
                log.warning("Couldn't read %s (%s); renamed it to %s.broken and skipped it", name, e, name)
                try:
                    os.replace(path, path + '.broken')
                except OSError:
                    pass
        return sorted(found, key=lambda j: (j.created_at, j.id))

    def unfinished(self):
        """Jobs to resume after a restart, oldest first."""
        return [j for j in self.all() if j.status in UNFINISHED]

    def finished_for(self, owner_id):
        """A user's done and stopped jobs, newest first."""
        return sorted((j for j in self.all(owner_id) if j.owner_id == owner_id and j.status in ('done', 'stopped')),
                      key=lambda j: (j.finished_at or j.created_at, j.id), reverse=True)

    def prune(self, owner_id, keep, jobs=None):
        """Deletes a user's ended jobs beyond the newest `keep`. Unfinished ones are never deleted."""
        ended = sorted((j for j in (jobs if jobs is not None else self.all(owner_id))
                        if j.owner_id == owner_id and j.finished),
                       key=lambda j: (j.finished_at or j.created_at, j.id), reverse=True)
        for job in ended[keep:]:
            self.delete(job)

    def prune_all(self, keep):
        """At startup: prunes every user's jobs, and deletes leftovers (half-written files, lists of ended jobs)."""
        jobs = self.all()
        for owner_id in {j.owner_id for j in jobs}:
            self.prune(owner_id, keep, jobs)
        for job in jobs:
            if job.finished:
                self.remove_list(job)
        try:
            for name in os.listdir(self.jobs_dir):
                if name.endswith('.tmp'):
                    os.remove(os.path.join(self.jobs_dir, name))
        except OSError as e:
            log.warning("Couldn't clean up %s: %s", self.jobs_dir, e)
