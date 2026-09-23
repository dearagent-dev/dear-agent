---
project: herald-lab
repos:
  - "rarguello/herald-lab-*"
enabled: true
base_branch: main
harness: opencode
verify_allow:
  - python -m unittest
  - make test
---

# Herald lab projects

Policy for the `rarguello/herald-lab-*` practice repositories.

The front matter above is the editable source of truth: run
`herald policy sync --dir policy` to project it into the database, where the worker reads
it. `verify_allow` **adds** to the operator's global `HERALD_VERIFY_ALLOW` allowlist for
these repos; it can never widen beyond running an allowlisted argv.
