"""The report can refresh itself, so the page never names a command nobody can run.

WHY THIS EXISTS (product-owner direction, 2026-08-31). The staleness banner
used to end with "Run ingest.py to refresh it before trusting the numbers
below." That sentence is true and completely unusable: the reader of this page
is somebody trying to get their token spend under control, sitting in a
browser, and `ingest.py` is a path they do not have, in a directory they were
never told, run with a `--db` flag they have never seen. A report that
diagnoses a problem and then names a remedy the reader cannot perform has not
finished the job.

So the page runs it. `POST /api/refresh` shells out to THE SAME `ingest.py`
command a user would type, over the database this server is already reading --
one implementation of "refresh", not a second one living in the server.

THE HONESTY RULE IS THE WHOLE TEST FILE. A refresh that fails, times out, or
cannot find `ingest.py` must SAY SO. The failure mode this guards against is
the one that matters: a green "refreshed!" over numbers that did not move,
which is strictly worse than the stale banner it replaced, because the reader
now believes the figures are current. `ok` is never true unless the ingest
process exited 0.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import serve as serve_mod  # noqa: E402
from ingest import default_projects_dir, ingest  # noqa: E402
from serve import Api, make_handler  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "session-fixture.jsonl"
PAGE = Path(__file__).resolve().parent.parent / "index.html"


class RefreshEndpointTest(unittest.TestCase):
    """The route exists, runs the real ingest, and reports what happened."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="cpb-refresh-test-"))
        projects = cls.tmp / "projects"
        projects.mkdir()
        shutil.copy(FIXTURE, projects / "session-fixture.jsonl")
        cls.db = cls.tmp / "usage.db"
        ingest(projects, cls.db)

        # A HOME OF OUR OWN, and it is the difference between testing this
        # code and testing this machine. `run_refresh` runs the same bare
        # `ingest.py --db ...` a user's button does, and that command derives
        # its transcript directory from `~/.claude/projects/<repo path>`. On
        # the author's laptop that directory is full, so the happy path passed;
        # on a CI runner it does not exist, so the refresh correctly REFUSED
        # and the test read that refusal as a bug in the feature.
        #
        # The refusal was right and the test was wrong: it asserted an
        # environment. So the child gets a temporary HOME with exactly one
        # transcript in exactly the directory the convention names, computed by
        # `default_projects_dir` rather than spelled out here -- a hand-written
        # copy of that convention would pass while disagreeing with the code
        # that implements it.
        cls.home = cls.tmp / "home"
        derived = default_projects_dir(cwd=Path.cwd(), home=cls.home)
        derived.mkdir(parents=True)
        shutil.copy(FIXTURE, derived / "session-fixture.jsonl")
        cls._real_home = os.environ.get("HOME")
        os.environ["HOME"] = str(cls.home)

        cls.api = Api(cls.db)
        cls.server = HTTPServer(("127.0.0.1", 0), make_handler(cls.api))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.api.conn.close()
        if cls._real_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = cls._real_home
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, path: str, host: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=120)
        try:
            headers = {"Host": host} if host else {}
            conn.request("POST", path, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, {"_raw": raw.decode("utf-8", "replace")}
        finally:
            conn.close()

    def get_status(self, path: str) -> int:
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        try:
            conn.request("GET", path)
            return conn.getresponse().status
        finally:
            conn.close()

    def stamp(self) -> float:
        # `ingest_runs` is a ONE-ROW table (`CHECK (id = 1)`), so a completed
        # run moves `finished_at` rather than appending. That column IS the
        # staleness verdict's own input, which makes it the right evidence
        # here: this asserts the thing the banner reads, not a proxy for it.
        return sqlite3.connect(self.db).execute(
            "SELECT finished_at FROM ingest_runs WHERE id = 1"
        ).fetchone()[0]

    def test_a_refresh_runs_the_ingest_and_stamps_the_database(self) -> None:
        # THE ACCEPTANCE CRITERION: the button does the thing. Not "the route
        # answers 200" -- the stamp the page's staleness verdict reads must
        # actually move, or the banner would clear over a database nothing
        # touched.
        before = self.stamp()
        status, payload = self.post("/api/refresh")
        self.assertEqual(status, 200, payload)
        self.assertIs(payload.get("ok"), True, payload)
        self.assertGreater(
            self.stamp(),
            before,
            "a refresh that answers ok:true must have advanced the ingest "
            "stamp -- otherwise the page reports fresh data over a database "
            "nothing touched",
        )

    def test_a_refresh_is_a_post_and_a_get_on_the_route_is_not_one(self) -> None:
        # A mutation must not sit behind GET: a browser prefetch, a history
        # revisit or a naive crawler would each silently start an ingest.
        self.assertEqual(self.get_status("/api/refresh"), 404)

    def test_the_host_guard_covers_the_mutation_too(self) -> None:
        # The DNS-rebinding guard on `do_GET` exists because a page in the
        # user's browser can reach this server by name. A mutation route that
        # skipped the check would be strictly worse than the read routes it
        # was added beside.
        status, _ = self.post("/api/refresh", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_an_unknown_post_route_is_not_a_refresh(self) -> None:
        status, _ = self.post("/api/nope")
        self.assertEqual(status, 404)


class RefreshRefusesRatherThanClaimingSuccessTest(unittest.TestCase):
    """A refresh that did not happen never answers `ok: true`.

    Each of these drives `run_refresh` directly, because the point is the
    RETURN SHAPE under failure and not the transport. The three failures are
    the three that actually occur: the script is missing (a partial install),
    the process fails (a stale schema, a permissions error), and the process
    never finishes (a corpus far larger than the timeout).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-refresh-refuse-test-"))
        self.db = self.tmp / "usage.db"
        self.db.write_bytes(b"")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_ingest_script_is_reported_not_swallowed(self) -> None:
        result = serve_mod.run_refresh(self.db, script=self.tmp / "no-such.py")
        self.assertIs(result["ok"], False)
        self.assertTrue(result["error"].strip())

    def test_a_failing_ingest_process_is_reported_with_its_own_words(self) -> None:
        # A script that exits non-zero and says why. The reader must get the
        # process's own message, not a generic "refresh failed" that sends
        # them nowhere.
        script = self.tmp / "boom.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write('the database schema is from a future build\\n')\n"
            "sys.exit(3)\n"
        )
        result = serve_mod.run_refresh(self.db, script=script)
        self.assertIs(result["ok"], False)
        self.assertIn("future build", result["error"])

    def test_a_refresh_that_never_finishes_times_out_rather_than_hanging(self) -> None:
        script = self.tmp / "hang.py"
        script.write_text("import time\ntime.sleep(30)\n")
        result = serve_mod.run_refresh(self.db, script=script, timeout=1.0)
        self.assertIs(result["ok"], False)
        self.assertIn("timed out", result["error"].lower())

    def test_a_successful_process_is_the_only_route_to_ok(self) -> None:
        script = self.tmp / "fine.py"
        script.write_text("print('done')\n")
        result = serve_mod.run_refresh(self.db, script=script)
        self.assertIs(result["ok"], True)


class ThePageOffersTheButtonRatherThanTheCommandTest(unittest.TestCase):
    """The page must ASK for the refresh, or the endpoint helps nobody.

    This is the "fully tested, never wired" pattern: an endpoint with green
    tests and no caller is not a feature. These assertions are the wiring.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_the_page_calls_the_refresh_route(self) -> None:
        self.assertIn("/api/refresh", self.html)

    def test_the_page_no_longer_tells_the_reader_to_run_a_command_it_can_run(
        self,
    ) -> None:
        # THE DEFECT, stated as a test. The staleness banner named a shell
        # command; the reader is in a browser. Any reappearance of that
        # sentence turns this red.
        self.assertNotIn("Run ingest.py to refresh it before trusting", self.html)

    def test_the_button_says_what_it_does_in_words_a_reader_already_knows(
        self,
    ) -> None:
        self.assertIn("Refresh now", self.html)


if __name__ == "__main__":
    unittest.main()
