"""Queue/API regressions: no Redis, model server, or production database needed."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import config
from app.db import connect, db_scope, now, transaction
from app.main import app
from app import tasks
from app.services import index_upload


class QueueContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-queue-tests-")
        self.addCleanup(self.directory.cleanup)
        self.db_context = db_scope(Path(self.directory.name) / "law_review.db")
        self.db_context.__enter__()
        self.addCleanup(self.db_context.__exit__, None, None, None)
        self.pool = SimpleNamespace(ping=AsyncMock(return_value=True), aclose=AsyncMock())
        self.pool_patch = patch("app.main.get_redis_pool", AsyncMock(return_value=self.pool))
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)
        self.case_id = self.client.post("/api/cases", json={"title": "队列隔离测试"}).json()["id"]

    def _stage_batch(self, count=2):
        queued = AsyncMock(return_value="test-job")
        with patch("app.main.enqueue_batch_import", queued):
            response = self.client.post(
                f"/api/cases/{self.case_id}/batch-import",
                files=[("files", (f"file-{i}.txt", f"文件 {i} 内容", "text/plain")) for i in range(count)],
            )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["batch_id"], queued.await_args.args[2]

    def test_readiness_refreshes_redis_and_preserves_core_availability(self):
        endpoint = '/api/system/health'
        self.assertEqual(self.client.get(endpoint).json()['redis'], 'available')
        self.pool.ping.side_effect = ConnectionError('private connection detail')
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['batch_import'], 'disabled')
        self.assertNotIn('private connection detail', response.text)
        self.pool.ping.side_effect = None
        self.assertEqual(self.client.get(endpoint).json()['batch_import'], 'enabled')
        self.assertEqual(self.pool.aclose.await_count, 4)

    def test_readiness_database_failure_and_not_started(self):
        with patch('app.main.sqlite3.connect', side_effect=RuntimeError('private database path')):
            response = self.client.get('/api/system/health')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['database'], 'unavailable')
        self.assertNotIn('private database path', response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with patch.object(app.state, 'ready', False):
            self.assertEqual(self.client.get('/api/system/health').status_code, 503)

    def test_readiness_does_not_create_missing_database(self):
        missing = Path(self.directory.name) / 'missing.db'
        with patch('app.main.get_db_path', return_value=missing):
            self.assertEqual(self.client.get('/api/system/health').status_code, 503)
        self.assertFalse(missing.exists())

    def test_lifespan_cancellation_cleans_executor_and_state(self):
        from app.main import lifespan
        async def run():
            application = SimpleNamespace(state=SimpleNamespace())
            with patch('app.main.init_db'), patch('app.main.start_review_executor'), patch('app.main.shutdown_review_executor') as shutdown:
                with patch('app.main.probe_redis', AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        async with lifespan(application):
                            self.fail('cancelled startup must not yield')
                shutdown.assert_called_once()
                self.assertFalse(application.state.ready)
                self.assertFalse(application.state.redis_available)
        with db_scope(Path(self.directory.name) / 'cancelled.db'):
            asyncio.run(run())
        from app.db import process_ownership
        with db_scope(Path(self.directory.name) / 'cancelled.db'), process_ownership():
            pass  # Cancelled startup released ownership.

    def test_second_owner_rejected_before_recovery(self):
        from app.main import lifespan
        async def run():
            application = SimpleNamespace(state=SimpleNamespace())
            with patch('app.main.init_db') as initialize, patch('app.main.start_review_executor') as start:
                with self.assertRaisesRegex(RuntimeError, 'already has a Web process owner'):
                    async with lifespan(application):
                        self.fail('second owner must not start')
                initialize.assert_not_called()
                start.assert_not_called()
                self.assertFalse(application.state.ready)
        asyncio.run(run())

    def test_redis_probe_timeout_and_cancellation_close_pool(self):
        from app.main import probe_redis
        async def run():
            for error in [TimeoutError(), asyncio.CancelledError()]:
                pool = SimpleNamespace(ping=AsyncMock(side_effect=error), aclose=AsyncMock())
                with patch('app.main.get_redis_pool', AsyncMock(return_value=pool)):
                    if isinstance(error, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await probe_redis()
                    else:
                        self.assertFalse(await probe_redis())
                pool.aclose.assert_awaited_once()
        asyncio.run(run())

    def test_health_pool_closed_and_batch_manifest_durable(self):
        self.pool.aclose.assert_awaited_once()
        batch_id, files = self._stage_batch()
        with closing(connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM batch_import_files WHERE batch_id=?", (batch_id,)).fetchone()[0]
        self.assertEqual(count, 2)
        self.assertTrue(all(Path(item["stored_path"]).exists() for item in files))

    def test_enqueue_uses_worker_queue_stable_id_and_closes_pool(self):
        async def run():
            pool = SimpleNamespace(enqueue_job=AsyncMock(return_value=SimpleNamespace(job_id="job-7")), aclose=AsyncMock())
            with patch("app.tasks.get_redis_pool", AsyncMock(return_value=pool)):
                self.assertEqual(await tasks.enqueue_batch_import(7, self.case_id, []), "job-7")
            self.assertEqual(pool.enqueue_job.await_args.kwargs["_queue_name"], tasks.WorkerSettings.queue_name)
            self.assertEqual(pool.enqueue_job.await_args.kwargs["_job_id"], "batch-import-7")
            pool.aclose.assert_awaited_once()
            pool.enqueue_job.side_effect = ConnectionError("offline")
            pool.aclose.reset_mock()
            with patch("app.tasks.get_redis_pool", AsyncMock(return_value=pool)):
                with self.assertRaises(ConnectionError):
                    await tasks.enqueue_batch_import(7, self.case_id, [])
            pool.aclose.assert_awaited_once()
        asyncio.run(run())

    def test_unconfirmed_enqueue_keeps_dispatch_pending_and_redispatches(self):
        with patch("app.main.enqueue_batch_import", AsyncMock(side_effect=ConnectionError("offline"))):
            response = self.client.post(f"/api/cases/{self.case_id}/batch-import",
                                        files=[("files", ("test.txt", "内容", "text/plain"))])
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertIn("自动重新派发", body["message"])
        batch_id = body["batch_id"]
        self.assertTrue(tasks.batch_temp_dir(batch_id).exists())
        with closing(connect()) as conn:
            batch = dict(conn.execute("SELECT * FROM batch_imports WHERE id=?", (batch_id,)).fetchone())
            dispatch = dict(conn.execute("SELECT * FROM batch_dispatch WHERE batch_id=?", (batch_id,)).fetchone())
            staged = conn.execute("SELECT COUNT(*) FROM batch_import_files WHERE batch_id=?", (batch_id,)).fetchone()[0]
        self.assertEqual(batch["status"], "queued")
        self.assertEqual(staged, 1)
        self.assertEqual(dispatch["state"], "pending")

        redispatched = AsyncMock(return_value="job-again")
        with patch("app.tasks.enqueue_batch_import", redispatched):
            count = asyncio.run(tasks.reconcile_batch_dispatches())
        self.assertEqual(count, 1)
        redispatched.assert_awaited_once()
        args = redispatched.await_args.args
        self.assertEqual(args[0], batch_id)
        self.assertEqual(args[1], self.case_id)
        self.assertEqual(len(args[2]), 1)
        with closing(connect()) as conn:
            dispatch = dict(conn.execute("SELECT * FROM batch_dispatch WHERE batch_id=?", (batch_id,)).fetchone())
        self.assertEqual(dispatch["state"], "discharged")
        self.assertEqual(dispatch["attempts"], 1)

    def test_reconcile_failure_stays_pending_and_worker_owned_batch_skipped(self):
        batch_id, _ = self._stage_batch(1)
        with transaction() as conn:
            conn.execute("UPDATE batch_dispatch SET state='pending', attempts=0, updated_at=?", (now(),))

        async def failed_dispatch(batch_id, case_id, files):
            raise ConnectionError("offline")

        with patch("app.tasks.enqueue_batch_import", failed_dispatch):
            self.assertEqual(asyncio.run(tasks.reconcile_batch_dispatches()), 0)
        with closing(connect()) as conn:
            dispatch = dict(conn.execute("SELECT * FROM batch_dispatch WHERE batch_id=?", (batch_id,)).fetchone())
        self.assertEqual(dispatch["state"], "pending")
        self.assertEqual(dispatch["attempts"], 1)

        # A batch already owned by a worker is never re-enqueued.
        with transaction() as conn:
            conn.execute("UPDATE batch_imports SET status='processing' WHERE id=?", (batch_id,))
            conn.execute("UPDATE batch_dispatch SET state='pending', updated_at=?", (now(),))
        redispatched = AsyncMock(return_value="job")
        with patch("app.tasks.enqueue_batch_import", redispatched):
            self.assertEqual(asyncio.run(tasks.reconcile_batch_dispatches()), 0)
        redispatched.assert_not_awaited()

    def test_lost_queue_ack_preserves_files_owned_by_worker(self):
        async def accepted_but_lost_ack(batch_id, case_id, files):
            with transaction() as conn:
                conn.execute("UPDATE batch_imports SET status='processing' WHERE id=?", (batch_id,))
            raise ConnectionError("acknowledgment lost")

        with patch("app.main.enqueue_batch_import", accepted_but_lost_ack):
            response = self.client.post(f"/api/cases/{self.case_id}/batch-import",
                                        files=[("files", ("test.txt", "内容", "text/plain"))])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "processing")
        self.assertTrue(tasks.batch_temp_dir(response.json()["batch_id"]).exists())

    def test_batch_aggregate_oversize_cleans_all_staging(self):
        queued = AsyncMock()
        with patch.object(config, "max_upload_total_size", 5), patch("app.main.enqueue_batch_import", queued):
            response = self.client.post(f"/api/cases/{self.case_id}/batch-import", files=[
                ("files", ("a.txt", b"123", "text/plain")), ("files", ("b.txt", b"456", "text/plain")),
            ])
        self.assertEqual(response.status_code, 413)
        queued.assert_not_called()
        self.assertEqual(list((config.upload_dir / "batch_temp").iterdir()), [])

    def test_normal_upload_aggregate_validated_before_any_index(self):
        with patch.object(config, "max_upload_total_size", 5):
            response = self.client.post(f"/api/cases/{self.case_id}/documents", files=[
                ("files", ("a.txt", b"123", "text/plain")), ("files", ("b.txt", b"456", "text/plain")),
            ])
        self.assertEqual(response.status_code, 413)
        with closing(connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM documents WHERE case_id=?", (self.case_id,)).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertFalse(list(config.upload_dir.glob("request-*")))

    def test_chunked_body_limit_before_multipart_parsing(self):
        body = b'--test\r\nContent-Disposition: form-data; name="files"; filename="a.txt"\r\n\r\n' + b"x" * (1024 * 1024 + 10) + b"\r\n--test--\r\n"
        with patch.object(config, "max_upload_total_size", 5):
            response = self.client.post(f"/api/cases/{self.case_id}/documents",
                                        headers={"Content-Type": "multipart/form-data; boundary=test"},
                                        content=iter([body[:500], body[500:]]))
        self.assertEqual(response.status_code, 413, response.text)

    def test_completed_batch_duplicate_delivery_is_noop(self):
        batch_id, files = self._stage_batch()
        first = asyncio.run(tasks.process_batch_import({}, batch_id, self.case_id, files))
        with patch("app.tasks.index_upload", side_effect=AssertionError("must not rerun")):
            second = asyncio.run(tasks.process_batch_import({}, batch_id, self.case_id, files))
        self.assertEqual(first, second)
        self.assertEqual(first["successful"], 2)
        self.assertFalse(tasks.batch_temp_dir(batch_id).exists())

    def test_commit_before_progress_crash_reuses_document(self):
        batch_id, files = self._stage_batch(1)
        item = files[0]
        existing = index_upload(self.case_id, item["filename"], Path(item["stored_path"]).read_bytes(), item["mime_type"],
                                import_key=f"batch:{batch_id}:{item['file_key']}")
        with transaction() as conn:
            conn.execute("UPDATE batch_import_files SET status='processing' WHERE batch_id=?", (batch_id,))
        result = asyncio.run(tasks.process_batch_import({}, batch_id, self.case_id, files))
        self.assertEqual(result["successful"], 1)
        with closing(connect()) as conn:
            documents = conn.execute("SELECT id FROM documents WHERE case_id=?", (self.case_id,)).fetchall()
        self.assertEqual([row["id"] for row in documents], [existing["id"]])

    def test_files_parallel_bounded_and_event_loop_responsive(self):
        batch_id, files = self._stage_batch(4)
        document = index_upload(self.case_id, "existing.txt", b"text", "text/plain")
        running = peak = 0
        lock = threading.Lock()

        def fake_index(*args, **kwargs):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1
            return document

        async def run():
            work = asyncio.create_task(tasks.process_batch_import({}, batch_id, self.case_id, files))
            ticks = 0
            while not work.done():
                await asyncio.sleep(0.005)
                ticks += 1
            result = await work
            self.assertGreater(ticks, 4)
            self.assertEqual(result["successful"], 4)

        with patch.object(config, "indexing_concurrency", 2), patch("app.tasks.index_upload", fake_index):
            asyncio.run(run())
        self.assertEqual(peak, 2)

    def test_partial_failure_terminal_progress_and_cleanup(self):
        batch_id, files = self._stage_batch(2)
        Path(files[1]["stored_path"]).unlink()
        result = asyncio.run(tasks.process_batch_import({}, batch_id, self.case_id, files))
        self.assertEqual((result["successful"], result["failed"]), (1, 1))
        response = self.client.get(f"/api/batch-imports/{batch_id}").json()
        self.assertEqual(response["status"], "completed_with_errors")
        self.assertEqual(response["progress_percent"], 100)
        self.assertFalse(tasks.batch_temp_dir(batch_id).exists())

    def test_cancellation_waits_for_inflight_file_and_resume_skips_it(self):
        batch_id, files = self._stage_batch(2)
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_index(*args, **kwargs):
            calls.append(kwargs["import_key"])
            started.set()
            if not release.wait(2):
                raise TimeoutError("test thread was not released")
            return index_upload(*args, **kwargs)

        async def run():
            work = asyncio.create_task(tasks.process_batch_import({}, batch_id, self.case_id, files))
            while not started.is_set():
                await asyncio.sleep(0.001)
            work.cancel()
            await asyncio.sleep(0.005)
            self.assertFalse(work.done(), "must not return while a write thread is still active")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
            self.assertTrue(tasks.batch_temp_dir(batch_id).exists())
            result = await tasks.process_batch_import({}, batch_id, self.case_id, files)
            self.assertEqual(result["successful"], 2)

        with patch.object(config, "indexing_concurrency", 1), patch("app.tasks.index_upload", slow_index):
            asyncio.run(run())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 2)

    def test_annotations_crud_ranges_and_evidence_delete(self):
        response = self.client.post(f"/api/cases/{self.case_id}/evidence", json={
            "title": "标注测试证据", "category": "书证", "fact": "待证事实", "quote": "原文一二三四五",
        })
        self.assertEqual(response.status_code, 201)
        evidence_id = response.json()["evidence"]["id"]
        endpoint = f"/api/evidence/{evidence_id}/annotations"
        self.assertEqual(self.client.post(endpoint, json={"content": "越界", "quote_start": 0, "quote_end": 50}).status_code, 400)
        response = self.client.post(endpoint, json={"content": "需要核验", "quote_start": 0, "quote_end": 2})
        self.assertEqual(response.status_code, 201, response.text)
        annotation_id = response.json()["id"]
        self.assertEqual(len(self.client.get(endpoint).json()), 1)
        endpoint_item = f"/api/evidence-annotations/{annotation_id}"
        self.assertEqual(self.client.patch(endpoint_item, json={"status": "已处理"}).json()["status"], "已处理")
        self.assertEqual(self.client.patch(endpoint_item, json={"quote_start": None}).status_code, 400)
        self.assertEqual(self.client.delete(endpoint_item).status_code, 200)
        self.assertEqual(self.client.delete(endpoint_item).status_code, 404)
        self.client.post(endpoint, json={"content": "再次标注"})
        response = self.client.delete(f"/api/evidence/{evidence_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted"]["annotations_deleted"], 1)
        self.assertEqual(self.client.get(endpoint).status_code, 404)


if __name__ == "__main__":
    unittest.main()
