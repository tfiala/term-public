"""Tests for the ADR corpus in docs/decisions.

The authoritative validator is `adr-lint` (from infra/acc, installed to
~/bin), run locally per docs/decisions/README.md.  It cannot run here:
it is distributed through a private Forgejo registry and is not
installable on this repo's GitHub runners.

These tests cover the part that goes stale in practice and needs no
external tool — the README index agreeing with the ADR files it points
at — plus the metadata vocabulary adr-lint would enforce, so a drifting
ADR is caught in CI rather than at the next local lint.
"""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DECISIONS = REPO_ROOT / "docs" / "decisions"
INDEX = DECISIONS / "README.md"

# adr-lint's ALLOWED_KINDS / ALLOWED_STATUSES.
ALLOWED_KINDS = {
    "decision", "proposal", "umbrella", "informational", "assessment",
    "incident_report", "record", "evidence", "legacy-conversion", "analysis",
}
ALLOWED_STATUSES = {
    "proposed", "accepted", "implemented", "validated", "rejected",
    "superseded", "deprecated", "deferred",
}
BINDING_KINDS = {"decision", "proposal", "umbrella"}

# adr-lint's REQUIRED_SECTIONS, for the kinds this repo uses so far.
REQUIRED_SECTIONS = {
    "decision": ["Context", "Decision", "Rationale", "Consequences",
                 "Evidence", "Revisit Triggers", "Alternatives Considered"],
}
INFRA_IMPACT_REQUIRED_FROM = "2026-05-04"

TITLE_RE = re.compile(r"^# ADR-(\d{4}): (.+)$")
FIELD_RE = re.compile(r"^\*\*(?P<key>[A-Za-z-]+):\*\* (?P<value>.+)$")
INDEX_ROW_RE = re.compile(
    r"^\| \[(?P<num>\d{4})\]\((?P<path>[^)]+)\) \| (?P<title>.+?) \| "
    r"(?P<status>.+?) \| (?P<revisit>.+?) \|$")


def adr_files():
    return sorted(p for p in DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))


def parse_adr(path):
    text = path.read_text()
    lines = text.splitlines()
    m = TITLE_RE.match(lines[0])
    assert m, f"{path.name}: first line must be '# ADR-NNNN: Title'"
    fields = {}
    for line in lines[1:]:
        if line.startswith("## "):
            break
        fm = FIELD_RE.match(line)
        if fm:
            fields[fm.group("key")] = fm.group("value")
    sections = re.findall(r"^## (.+)$", text, re.M)
    return {"number": m.group(1), "title": m.group(2),
            "fields": fields, "sections": sections, "text": text}


def index_rows():
    rows = {}
    for line in INDEX.read_text().splitlines():
        m = INDEX_ROW_RE.match(line)
        if m:
            rows[m.group("path")] = m.groupdict()
    return rows


class TestCorpusExists:

    def test_index_present(self):
        assert INDEX.is_file(), "docs/decisions/README.md is the ADR index"

    def test_at_least_one_adr(self):
        assert adr_files(), "no ADRs found in docs/decisions"


class TestAdrMetadata:
    """Each ADR carries the metadata and sections adr-lint requires."""

    def test_filename_matches_number(self):
        for path in adr_files():
            adr = parse_adr(path)
            assert path.name.startswith(adr["number"] + "-"), (
                f"{path.name}: title number {adr['number']} != filename")

    def test_numbers_unique(self):
        """Duplicate numbers make every ADR-NNNN citation ambiguous."""
        seen = {}
        for path in adr_files():
            num = parse_adr(path)["number"]
            assert num not in seen, (
                f"ADR {num} claimed by both {seen.get(num)} and {path.name}")
            seen[num] = path.name

    def test_required_fields_and_vocabulary(self):
        for path in adr_files():
            fields = parse_adr(path)["fields"]
            for key in ("Kind", "Status", "Date", "Revisit"):
                assert key in fields, f"{path.name}: missing **{key}:**"
            assert fields["Kind"] in ALLOWED_KINDS, (
                f"{path.name}: Kind {fields['Kind']!r} not in adr-lint vocabulary")
            assert fields["Status"] in ALLOWED_STATUSES, (
                f"{path.name}: Status {fields['Status']!r} not in adr-lint "
                "vocabulary (it is lowercase and carries no progress prose)")
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["Date"]), (
                f"{path.name}: Date must be YYYY-MM-DD")

    def test_required_sections_present(self):
        for path in adr_files():
            adr = parse_adr(path)
            kind = adr["fields"].get("Kind")
            for section in REQUIRED_SECTIONS.get(kind, []):
                assert section in adr["sections"], (
                    f"{path.name}: missing required section '## {section}' "
                    f"for kind {kind}")

    def test_infra_impact_on_recent_binding_adrs(self):
        """Binding ADRs dated on/after the cutoff need '## Infra Impact';
        'None.' is the accepted body and the usual answer here."""
        for path in adr_files():
            adr = parse_adr(path)
            fields = adr["fields"]
            if (fields.get("Kind") in BINDING_KINDS
                    and fields.get("Date", "") >= INFRA_IMPACT_REQUIRED_FROM):
                assert "Infra Impact" in adr["sections"], (
                    f"{path.name}: binding ADR dated {fields['Date']} needs "
                    "'## Infra Impact'")

    def test_no_section_is_empty(self):
        """An empty section reads as covered but says nothing."""
        for path in adr_files():
            parts = re.split(r"^## .+$", parse_adr(path)["text"], flags=re.M)
            for body in parts[1:]:
                assert body.strip(), f"{path.name}: has an empty '## ' section"


class TestIndexAgreesWithFiles:
    """Both ends of the index contract — the README is the entry point,
    so a stale row misroutes every reader who starts there."""

    def test_every_adr_is_indexed(self):
        rows = index_rows()
        for path in adr_files():
            assert path.name in rows, (
                f"{path.name} is not listed in docs/decisions/README.md")

    def test_no_index_row_without_a_file(self):
        for name in index_rows():
            assert (DECISIONS / name).is_file(), (
                f"README index points at missing file {name}")

    def test_row_number_matches_linked_file(self):
        for name, row in index_rows().items():
            assert name.startswith(row["num"] + "-"), (
                f"index row {row['num']} links to {name}")

    def test_titles_and_statuses_match(self):
        rows = index_rows()
        for path in adr_files():
            adr = parse_adr(path)
            row = rows[path.name]
            assert row["title"].strip() == adr["title"], (
                f"{path.name}: index title is stale\n"
                f"  index: {row['title'].strip()}\n"
                f"  file:  {adr['title']}")
            assert row["status"].strip() == adr["fields"]["Status"], (
                f"{path.name}: index status is stale "
                f"({row['status'].strip()!r} vs {adr['fields']['Status']!r})")
