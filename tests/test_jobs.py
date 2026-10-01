"""
Tests for the job store: scans saved on disk so a restart resumes them.

Run from the repository root:  python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import jobs  # noqa: E402


class StoreTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = jobs.JobStore.open(self.dir.name)
        self.assertIsNotNone(self.store)

    def new(self, owner=1, total=3, **changes):
        job = self.store.new_job(owner, f'<@{owner}>', 10, 100, 'java', True,
                                 {'kind': 'file', 'description': 'ips.txt'}, total)
        for key, value in changes.items():
            setattr(job, key, value)
        return job

    def files(self):
        return sorted(os.listdir(self.store.jobs_dir))

    def test_a_saved_job_reads_back_the_same(self):
        job = self.new(cursor={'phase': 'api', 'index': 2}, blocked=['router.lan'], unchecked=1,
                       results={'1.2.3.4': {'ip': '1.2.3.4', 'players': 3, 'names': ['Steve'], 'source': 'direct'}},
                       timings={'direct': [3, 1.5], 'api': None}, elapsed=12.5)
        self.assertTrue(self.store.save(job))
        [back] = self.store.all()
        self.assertEqual(back, job)
        self.assertEqual(self.files(), [job.id + '.json'])  # No .tmp left behind

    def test_the_id_sorts_by_time_and_names_the_owner(self):
        job = self.new(owner=42)
        self.assertRegex(job.id, r'^\d{8}T\d{6}Z-42-[0-9a-f]{4}$')
        self.assertTrue(job.created_at.endswith('Z'))

    def test_the_list_is_written_once_and_read_back(self):
        job = self.new()
        self.assertTrue(self.store.write_list(job, ['1.2.3.4', 'mc.example.com:25566', '5.6.7.8']))
        self.assertEqual(self.store.read_list(job), ['1.2.3.4', 'mc.example.com:25566', '5.6.7.8'])
        self.store.remove_list(job)
        self.assertIsNone(self.store.read_list(job))
        self.store.remove_list(job)  # Already gone: no error

    def test_a_file_that_cant_be_read_is_renamed_and_skipped(self):
        good = self.new()
        self.store.save(good)
        with open(os.path.join(self.store.jobs_dir, 'broken.json'), 'w') as f:
            f.write('{"id": ')
        with open(os.path.join(self.store.jobs_dir, 'list.json'), 'w') as f:
            f.write('[1, 2]')
        with self.assertLogs('scanbot', 'WARNING') as logs:
            self.assertEqual([j.id for j in self.store.all()], [good.id])
        self.assertEqual(len(logs.records), 2)
        self.assertIn('broken.json.broken', self.files())
        self.assertIn('list.json.broken', self.files())

    def test_a_job_from_a_newer_version_is_left_alone(self):
        job = self.new(version=jobs.FORMAT_VERSION + 1)
        self.store.save(job)
        with self.assertLogs('scanbot', 'WARNING'):
            self.assertEqual(self.store.all(), [])
        self.assertEqual(self.files(), [job.id + '.json'])

    def test_unknown_fields_are_ignored(self):
        job = self.new()
        self.store.save(job)
        path = self.store.path(job)
        with open(path) as f:
            data = json.load(f)
        data['added_later'] = 1
        with open(path, 'w') as f:
            json.dump(data, f)
        self.assertEqual(self.store.all(), [job])

    def test_unfinished_and_finished_jobs(self):
        made = {status: self.new(status=status) for status in
                ('queued', 'running', 'interrupted', 'done', 'stopped', 'abandoned')}
        for i, job in enumerate(made.values()):
            job.created_at = f'2026-10-01T00:00:0{i}Z'
            job.finished_at = job.created_at if job.finished else None
            self.store.save(job)
        self.assertEqual([j.status for j in self.store.unfinished()], ['queued', 'running', 'interrupted'])
        self.assertEqual([j.status for j in self.store.finished_for(1)], ['stopped', 'done'])  # Newest first
        self.assertEqual(self.store.finished_for(2), [])

    def test_one_users_jobs_are_found_without_reading_anyone_elses(self):
        mine = self.new(owner=1, status='done', finished_at='2026-10-01T00:00:00Z')
        self.store.save(mine)
        other = self.new(owner=12, status='done')
        broken = os.path.join(self.store.jobs_dir, other.id + '.json')
        with open(broken, 'w') as f:
            f.write('{"id": ')  # Would be renamed .broken if it were read
        self.assertEqual([j.id for j in self.store.finished_for(1)], [mine.id])
        self.store.prune(1, keep=1)
        self.assertIn(other.id + '.json', self.files())

    def test_prune_keeps_each_users_newest_ended_jobs_and_every_unfinished_one(self):
        kept, deleted = [], []
        for i in range(4):
            job = self.new(status='done', finished_at=f'2026-10-01T00:00:0{i}Z')
            self.store.write_list(job, ['1.2.3.4'])  # A leftover: ended jobs don't need theirs
            self.store.save(job)
            (kept if i >= 2 else deleted).append(job.id)
        running = self.new(status='running')
        other = self.new(owner=2, status='done', finished_at='2026-09-01T00:00:00Z')
        for job in (running, other):
            self.store.save(job)
            self.store.write_list(job, ['1.2.3.4'])
        open(os.path.join(self.store.jobs_dir, 'x.json.tmp'), 'w').close()  # A save cut short

        self.store.prune_all(keep=2)

        ids = {j.id for j in self.store.all()}
        self.assertEqual(ids, set(kept) | {running.id, other.id})
        self.assertIsNotNone(self.store.read_list(running))  # Still needed
        self.assertIsNone(self.store.read_list(other))
        self.assertFalse([name for name in self.files() if name.endswith('.tmp')])

    def test_open_returns_none_and_says_how_to_fix_it_when_the_folder_cant_be_written(self):
        blocker = os.path.join(self.dir.name, 'a-file')
        open(blocker, 'w').close()
        with self.assertLogs('scanbot', 'WARNING') as logs:
            self.assertIsNone(jobs.JobStore.open(os.path.join(blocker, 'state')))  # A folder inside a file
        self.assertIn("a restart ends them instead of resuming them", logs.output[0])
        self.assertTrue('chown' in logs.output[0] or 'STATE_DIR' in logs.output[0])

    def test_a_failed_save_warns_once_and_doesnt_raise(self):
        job = self.new()
        self.store.jobs_dir = os.path.join(self.dir.name, 'gone', 'jobs')
        with self.assertLogs('scanbot', 'WARNING') as logs:
            self.assertFalse(self.store.save(job))
            self.assertFalse(self.store.save(job))
        self.assertEqual(len(logs.records), 1)


if __name__ == '__main__':
    unittest.main()
