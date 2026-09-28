# Requirements

What this service must do, stated so that a test can name it.

This directory is new. Most of what the service does today is described in the other pages
of `docs/` and has no identifier yet; a requirement is added here when a behaviour needs one,
which is first of all when it is decided before it is built.

| Page | Covers |
|---|---|
| [eligibility-and-areas.md](eligibility-and-areas.md) | Areas declared as primary-substation boundaries; eligibility and the member's area decided by boundary; what a submission keeps and who resolves it; re-resolution at approval; the anonymous route's answer and rate limit; what template import refuses; the checked supply address the boundary is resolved from; find-by-address under a Digital Twin outage, and its `unchecked` flag; no coordinate in either anonymous answer; the review naming the area by its display name |
| [client-address.md](client-address.md) | The address recorded as consent evidence and in the audit trail, and why no forwarded header is read for it |
| [registry-member.md](registry-member.md) | The identity approval's registry member client authenticates as, with and without the dataspace |
| [registry-sync.md](registry-sync.md) | The explicit, platform-admin push of a template's areas to the REC registry (route and CLI), its dry run, prune and refusals, a renamed area moved with its members, an area's display name, the community set-up step it starts with, and the console's drift check and who sees it |

## Identifiers

`REQ-` followed by four digits, from `REQ-0001`, never reused. Each requirement is a
heading of the form `### REQ-0001 — <the behaviour, as a statement>`. A trailing slug is
cosmetic.

## Planned and implemented

**A requirement may land before the code that satisfies it**, marked with a status line
directly under its heading:

```markdown
### REQ-0003 — …

**Status:** planned
```

- **`planned`** describes behaviour this service does not have yet. It is written as the
  behaviour it will be, so that the change delivering it has something to be measured
  against, and so that the decision it rests on is readable before the code is. No test
  claims it yet.
- **A requirement with no status line is implemented**: the code does it today.
- **A planned requirement turns implemented in the change whose tests make it pass.** That
  change removes the status line and adds the tests that name it. It is never marked
  implemented ahead of those tests.
- **An implemented requirement never describes behaviour the code lacks.** When a decision
  changes something the service already does, the current requirement stays true and the
  change is added beside it as a new planned requirement.

## How a requirement is verified

A test declares what it covers with a `@verifies REQ-####` tag in its docstring, **on a
line of its own** (one tag per line; several may follow one another):

```python
async def test_an_address_outside_every_boundary_is_not_eligible(client):
    """
    @verifies REQ-0003
    """
```

The harness checker reads a tag only where it opens its line, so a tag written on the
docstring's opening line (`"""@verifies REQ-0003"""`) is not seen by it. The mapping is a
projection of the two and is never written by hand. Besides the harness checker, the
projection is a grep:

```bash
grep -rho --include='*.py' "@verifies REQ-[0-9]\{4\}" tests/ | sort | uniq -c
grep -rhoE '^### (REQ-[0-9]{4})' docs/specifications/*.md | sort
```

A planned requirement with no test is expected and is not reported as uncovered. A tag
naming an identifier that no page here declares is an error.
