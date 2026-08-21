"""Whether a fresh clone can still say what this project has achieved.

The scenario this defends is a machine move. `git` carries the code and the
manifest; the stock APKs are kept deliberately; **`work/` — 103 GB of decodes and
builds — does not travel.** So the question is what survives, and the answer has
to be checked rather than assumed, because it has already been wrong once:
`static_verified` lived only in gitignored `work/`, and Instagram 441 read 4 of 7
release-ready on the machine that ported it and **0 of 7** from a clean checkout.

Two things are asserted here, and they fail differently on purpose.

**Readiness reproduces from the committed tree alone.** Not from `manifest/` as it
sits on disk — from `git archive HEAD`, which is what a clone actually receives.
A file that is present but untracked passes every other test in this suite and
then is not there. That is exactly the shape of the bug above.

**Every file the expectation reads is tracked.** The first test would catch an
untracked file only as a changed number, and only if it happened to change one.
This one names the file. Both are cheap; only one of them is actionable at 3 a.m.

Neither can be satisfied by re-deriving anything. `runtime_probe` and
`differential` come from a device session, and a device session cannot be
replayed — a 439 build installed today is measured against a 2026 server, and
Instagram decides behaviour server-side (a MobileConfig flag picks which settings
implementation loads). So for two of the three kinds a hook needs after a build,
**the committed file is the only copy there will ever be.** Everything else in
this repo is a cache; this is not.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline import expectation
from dfinsta_pipeline.final_report import build_report, read_claims

REPOSITORY = Path(__file__).resolve().parent.parent

#: `version -> (release-ready, hooks)` as the committed evidence reports it.
#: Only ever appended to, one row per ported version. A row changing is either a
#: real regression or evidence that stopped travelling, and both want a human.
#:
#: 439 has no row: `static_verified` had no producer until 440, so 439's
#: readiness is not computable from any corpus and never will be.
KNOWN_READINESS = {
    "440": (2, 7),
    "441": (4, 7),
}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY), *args], capture_output=True, text=True
    )


def _requires_a_work_tree(case: unittest.TestCase) -> None:
    """Skip where there is no git, and say so rather than passing quietly.

    Out-of-tree mutation copies exclude `.git`, and a test about what git carries
    cannot mean anything without it. A skip is honest here; a pass would not be.
    """

    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        case.skipTest("not a git work tree — nothing can be said about what a clone gets")


class CommittedTreeTests(unittest.TestCase):
    def test_readiness_reproduces_from_the_committed_tree_alone(self) -> None:
        """`git archive HEAD`, extracted cold, must give the same numbers.

        Reading `manifest/` in place would pass with an untracked file in it,
        which is the failure being guarded. This reads what a clone receives.
        """

        _requires_a_work_tree(self)
        # Binary, not `text=True`: a tar decoded as UTF-8 and re-encoded is not
        # the same tar, and the failure looks like a corrupt archive rather than
        # like the mistake it is.
        archive = subprocess.run(
            ["git", "-C", str(REPOSITORY), "archive", "--format=tar", "HEAD", "manifest"],
            capture_output=True,
        )
        self.assertEqual(0, archive.returncode, archive.stderr.decode("utf-8", "replace"))

        with tempfile.TemporaryDirectory() as raw:
            clone = Path(raw)
            tar_path = clone / "manifest.tar"
            tar_path.write_bytes(archive.stdout)
            with tarfile.open(tar_path) as tar:
                tar.extractall(clone, filter="data")
            tar_path.unlink()

            found: dict[str, tuple[int, int]] = {}
            series = expectation.versions_with_evidence(clone)
            for version in series:
                if version not in KNOWN_READINESS:
                    continue
                previous = [v for v in series if int(v) < int(version)]
                report = build_report(
                    version,
                    read_claims(
                        expectation.evidence_files(
                            clone, version, previous[-1] if previous else None
                        )
                    ),
                )
                found[version] = (len(report.ready), len(report.hooks))

        # The positive control. A clone missing every evidence file would produce
        # an empty `found`, and an empty comparison against an empty expectation
        # is the vacuous pass this whole file exists to prevent.
        self.assertEqual(
            KNOWN_READINESS,
            found,
            "readiness from the committed tree differs from what was recorded. "
            "Either a port regressed, or evidence stopped being committed — the "
            "second is silent everywhere else.",
        )

    def test_every_evidence_file_the_expectation_reads_is_tracked(self) -> None:
        """Names the file, where the test above would only name a number."""

        _requires_a_work_tree(self)

        def tracked(path: Path) -> bool:
            return (
                _git("ls-files", "--error-unmatch", str(path.relative_to(REPOSITORY))).returncode
                == 0
            )

        # Control first: the check must be able to say no. A `git ls-files` that
        # returned 0 for everything would make the loop below unfalsifiable, and
        # this suite has shipped an assertion that could not fail before.
        self.assertFalse(
            tracked(REPOSITORY / "manifest" / "static_evidence" / "not-a-real-file.jsonl"),
            "the tracked-file check cannot report an untracked path",
        )

        series = expectation.versions_with_evidence(REPOSITORY)
        checked: list[Path] = []
        untracked: list[str] = []
        for index, version in enumerate(series):
            previous = series[index - 1] if index else None
            for path in expectation.evidence_files(REPOSITORY, version, previous):
                if not path.is_file():
                    continue
                checked.append(path)
                if not tracked(path):
                    untracked.append(str(path.relative_to(REPOSITORY)))

        self.assertGreater(len(checked), 3, "too few evidence files found to mean anything")
        self.assertEqual(
            [],
            untracked,
            "evidence the expectation reads is not committed. It works here and "
            "vanishes on clone — 441 read 4 of 7 locally and 0 of 7 from a clean "
            "checkout the last time this happened.",
        )


class BootstrapRecordTests(unittest.TestCase):
    """The bootstrap list, checked against the code rather than trusted.

    The README names what a fresh clone has to be given before anything runs. A
    list like that rots the moment a default changes, and its readers are by
    definition people who cannot yet run anything to find out.

    It lived in `docs/BOOTSTRAP.md` until 2026-08-21, when that file and
    `docs/RUNNING_A_PORT.md` were folded into the README so a new reader has one
    place to be. Both assertions below moved with it.
    """

    def test_the_bootstrap_list_names_what_the_code_actually_wants(self) -> None:
        text = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        driver = (REPOSITORY / "src" / "dfinsta_pipeline" / "driver.py").read_text(
            encoding="utf-8"
        )

        # The apktool filename is a hard-coded default, so the doc quoting a
        # different one is a reader downloading the wrong jar.
        self.assertIn("apktool_2.9.3.jar", driver)
        self.assertIn("apktool_2.9.3.jar", text)

        for required in (
            "framework-res",          # only ever existed under gitignored work/
            "DFINSTA_KEYSTORE",       # signing identity, never in the repo
            "platform-tools",         # adb
            "build-tools",            # apksigner, zipalign, aapt
            "temporal",               # the durable orchestration, optional
        ):
            self.assertIn(required, text, f"the README does not mention {required}")

    def test_the_readme_prerequisites_survive(self) -> None:
        """A README whose requirements section is gone is worse than none.

        Checked by either heading: the section was renamed from `Prerequisites`
        to `Requirements` on 2026-08-17, and the rule is that a reader arriving
        at a fresh clone is told what they need — not that the heading has a
        particular word in it.
        """

        text = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        self.assertTrue(
            "## Requirements" in text or "## Prerequisites" in text,
            "the README names no requirements section under either heading",
        )
        # The three things a clone cannot be given and must be told about.
        for needed in ("apktool_2.9.3.jar", "framework-res", "DFINSTA_KEYSTORE"):
            self.assertIn(needed, text, f"the README does not mention {needed}")
        # Python is pinned in pyproject and stated in the README; the two
        # disagreeing sends a new machine to an unsupported interpreter.
        pyproject = (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.11,<3.14"', pyproject)
        self.assertIn("3.13", text)


if __name__ == "__main__":
    unittest.main()
